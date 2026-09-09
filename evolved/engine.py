# evolved/engine.py - Evolved review engine (contract D, Qt-free core).
"""Accepted-answer credit path for Evolved. Called from
reviewer_did_answer_card AFTER Anki accepts the answer (never from the
pre-scheduling _answerCard wrapper). Versions adapters for Anki hook shapes.

Flow per accepted answer:
  1. Build review identity (revlog id/card id) -> review_key.
  2. Eligibility check (ratings 2/3/4, kinds, activation baseline).
  3. Duplicate delivery reuses the same local operation (idempotent).
  4. Persist operation to the journal BEFORE showing any durable award.
  5. Replay canonical set -> new projection; return HUD/menu updates.
Catch-up scans run off the review-critical path in bounded chunks.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import reviews as _reviews
from .reducer import replay as _replay


@dataclass
class EngineConfig:
    game_uuid: str
    device_id: str
    activated_at: int
    rules: Dict[str, Any]
    rules_version: int = 1
    lamport_start: int = 0


@dataclass
class EngineState:
    lamport: int = 0
    processed_keys: set = field(default_factory=set)
    # Bounded recent-credit index for Undo reconciliation: review_key ->
    # (revlog_id, card_id). Retraction delivery is idempotent in the
    # reducer, so a bounded window is safe (older keys reconcile on next
    # full catch-up scan instead).
    recent: Dict[str, tuple] = field(default_factory=dict)
    retracted_for_undo: set = field(default_factory=set)

RECENT_WINDOW = 50


class EvolvedEngine:
    def __init__(self, config: EngineConfig, journal):
        self.cfg = config
        self.journal = journal
        self.state = EngineState(lamport=config.lamport_start)

    def _next_lamport(self) -> int:
        self.state.lamport += 1
        return self.state.lamport

    def credit_direct(self, *, revlog_id: int, card_id: int, ease: int,
                      revlog_type: int, review_ts: int, skill: str,
                      resource: str, preview=False, cancelled=False) -> Dict[str, Any]:
        """Credit one accepted direct review. Idempotent per review identity."""
        eligible = _reviews.is_eligible(
            ease=ease, revlog_type=revlog_type, review_ts=review_ts,
            activated_at=self.cfg.activated_at, preview=preview, cancelled=cancelled)
        if not eligible:
            return {"ok": True, "awarded": False, "reason": "ineligible"}
        key = _reviews.make_review_key(self.cfg.game_uuid, revlog_id, card_id)
        fp = _reviews.immutable_fingerprint(
            revlog_id=revlog_id, card_id=card_id, ease=ease,
            review_ts=review_ts, revlog_type=revlog_type)
        if key in self.state.processed_keys:
            return {"ok": True, "awarded": False, "reason": "duplicate_delivery", "review_key": key}
        op = {"op_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.cfg.game_uuid}:{key}:direct")),
              "game_uuid": self.cfg.game_uuid, "device_id": self.cfg.device_id,
              "device_seq": self.journal.allocate_seq(self.cfg.device_id),
              "lamport": self._next_lamport(), "kind": "review_award",
              "payload": {"review_key": key, "review_ts": int(review_ts), "rating": int(ease),
                          "review_kind": "review", "provenance": "direct",
                          "skill": str(skill).lower(), "resource": str(resource)}}
        try:
            self.journal.append_operation(op)
        except Exception as exc:
            return {"ok": False, "awarded": False, "error": f"persist_failed:{exc!r}",
                    "needs_recovery": True}
        self.journal.observe_review(key, revlog_id, card_id, fp)
        self.state.processed_keys.add(key)
        self._track_recent(key, revlog_id, card_id)
        projection = _replay(self._all_ops(), self.cfg.rules, self.cfg.game_uuid)
        return {"ok": True, "awarded": True, "review_key": key, "projection": projection}

    def scan_catchup(self, history: List[Dict[str, Any]], *, chunk: int = 500,
                     preset_skill: str = "mining") -> Dict[str, Any]:
        """Chunked catch-up ingestion. Caller persists processed identities
        transactionally with awards (journal.observe_review per item)."""
        _ = preset_skill
        eligible, stats = _reviews.find_uncredited(history, self.state.processed_keys,
                                                   self.cfg.activated_at)
        made = 0
        for row in eligible[:max(1, chunk)]:
            key = row.get("review_key") or _reviews.make_review_key(
                self.cfg.game_uuid, int(row["revlog_id"]), int(row["card_id"]))
            op = {"op_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.cfg.game_uuid}:{key}:catchup")),
                  "game_uuid": self.cfg.game_uuid, "device_id": self.cfg.device_id,
                  "device_seq": self.journal.allocate_seq(self.cfg.device_id),
                  "lamport": self._next_lamport(), "kind": "review_award",
                  "payload": {"review_key": key, "review_ts": int(row.get("ts", 0)),
                              "rating": int(row.get("ease", 3)), "review_kind": "review",
                              "provenance": "catchup"}}
            try:
                self.journal.append_operation(op)
            except Exception:
                continue  # duplicate delivery reuses same op_id; skip
            self.journal.observe_review(key, int(row["revlog_id"]), int(row["card_id"]),
                                        _reviews.immutable_fingerprint(
                                            revlog_id=int(row["revlog_id"]),
                                            card_id=int(row["card_id"]),
                                            ease=int(row.get("ease", 3)),
                                            review_ts=int(row.get("ts", 0)),
                                            revlog_type=int(row.get("revlog_type", 1))))
            self.state.processed_keys.add(key)
            self._track_recent(key, int(row["revlog_id"]), int(row["card_id"]))
            made += 1
        return {"made": made, "stats": stats}

    def _track_recent(self, key: str, revlog_id: int, card_id: int) -> None:
        self.state.recent[key] = (int(revlog_id), int(card_id))
        while len(self.state.recent) > RECENT_WINDOW:
            self.state.recent.pop(next(iter(self.state.recent)))

    def hydrate(self) -> int:
        """Reload processed identities + recent window from the journal so a
        restart neither re-ingests the same history nor litters duplicate
        catch-up operations. Returns the observation count."""
        try:
            rows = self.journal._conn.execute(
                "SELECT review_key, revlog_id, card_id FROM review_observations"
                " ORDER BY rowid").fetchall()
        except Exception:
            return 0
        for row in rows:
            try:
                key = row["review_key"]
            except (KeyError, TypeError, IndexError):
                continue
            self.state.processed_keys.add(key)
        for row in rows[-RECENT_WINDOW:]:
            try:
                self.state.recent[row["review_key"]] = (
                    int(row["revlog_id"]), int(row["card_id"]))
            except (KeyError, ValueError, TypeError):
                continue
        return len(rows)

    def reconcile_undo(self, row_exists) -> Dict[str, Any]:
        """Reconcile recent awards against Anki history after an Undo/Redo.

        row_exists(revlog_id) -> bool, injected for tests. A missing row
        retracts that review's award (practice-safe: duplicate retractions
        are idempotent downstream); a reappeared row restores an
        undo-retraction. A replacement answer has a new identity and earns
        normally through the regular credit path.
        """
        retracted, restored = [], []
        for key, (revlog_id, _card_id) in list(self.state.recent.items()):
            try:
                present = bool(row_exists(revlog_id))
            except Exception:
                continue
            if not present and key not in self.state.retracted_for_undo:
                try:
                    self.retract(review_key=key, reason="anki_undo")
                except Exception:
                    continue
                self.state.retracted_for_undo.add(key)
                retracted.append(key)
            elif present and key in self.state.retracted_for_undo:
                try:
                    self.restore(review_key=key, reason="anki_redo")
                except Exception:
                    continue
                self.state.retracted_for_undo.discard(key)
                restored.append(key)
        return {"retracted": retracted, "restored": restored}

    def _retract_state(self, review_key: str) -> str:
        """Latest retract/restore disposition for key: 'retracted',
        'active', or 'unknown' (no retraction history)."""
        try:
            rows = self.journal._conn.execute(
                "SELECT kind FROM operations WHERE kind IN"
                " ('review_retract', 'review_restore') AND"
                " json_extract(payload_json, '$.target_review_key') = ?"
                " ORDER BY lamport DESC, device_id, device_seq LIMIT 1",
                (review_key,)).fetchall()
        except Exception:
            return "unknown"
        if not rows:
            return "active"
        return "retracted" if rows[0]["kind"] == "review_retract" else "active"

    def retract(self, *, review_key: str, reason: str = "anki_undo") -> Dict[str, Any]:
        if self._retract_state(review_key) == "retracted":
            self.state.retracted_for_undo.add(review_key)
            return {"ok": True, "review_key": review_key, "deduped": True}
        op = {"op_id": str(uuid.uuid4()), "game_uuid": self.cfg.game_uuid,
              "device_id": self.cfg.device_id,
              "device_seq": self.journal.allocate_seq(self.cfg.device_id),
              "lamport": self._next_lamport(), "kind": "review_retract",
              "payload": {"target_review_key": review_key, "reason": reason}}
        self.journal.append_operation(op)
        return {"ok": True, "review_key": review_key}

    def restore(self, *, review_key: str, reason: str = "anki_redo") -> Dict[str, Any]:
        if self._retract_state(review_key) == "active":
            self.state.retracted_for_undo.discard(review_key)
            return {"ok": True, "review_key": review_key, "deduped": True}
        op = {"op_id": str(uuid.uuid4()), "game_uuid": self.cfg.game_uuid,
              "device_id": self.cfg.device_id,
              "device_seq": self.journal.allocate_seq(self.cfg.device_id),
              "lamport": self._next_lamport(), "kind": "review_restore",
              "payload": {"target_review_key": review_key, "reason": reason}}
        self.journal.append_operation(op)
        return {"ok": True, "review_key": review_key}

    def _all_ops(self) -> List[Dict[str, Any]]:
        # Journal stores payload_json text; rehydrate to operation dicts.
        import json

        rows = self.journal._conn.execute(
            "SELECT op_id, game_uuid, device_id, device_seq, lamport, kind, payload_json"
            " FROM operations").fetchall()
        ops = []
        for r in rows:
            ops.append({"op_id": r["op_id"], "game_uuid": r["game_uuid"],
                        "device_id": r["device_id"], "device_seq": r["device_seq"],
                        "lamport": r["lamport"], "kind": r["kind"],
                        "payload": json.loads(r["payload_json"])})
        return ops
