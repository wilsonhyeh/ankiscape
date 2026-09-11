# evolved/engine.py - Evolved review engine (contract D, Qt-free core).
"""Accepted-answer credit path for Evolved. Called from
reviewer_did_answer_card AFTER Anki accepts the answer (never from the
pre-scheduling _answerCard wrapper). Versions adapters for Anki hook shapes.

Flow per accepted answer:
  1. Build review identity (revlog id/card id) -> review_key.
  2. Eligibility check (ratings 2/3/4, kinds, activation baseline).
  3. Duplicate delivery reuses the same committed identity (idempotent).
  4. ONE journal transaction persists the operation (with its device
     sequence), outbox entry and review observation before any award shows.
     The interactive write fails fast (25 ms busy budget) instead of
     waiting behind a long lock.
  5. Projection is derived work: with a ProjectionWorker attached, this call
     returns immediately and the worker publishes the new state; without one
     (tests/pure use) the cached projection extends incrementally here.
Catch-up scans run off the review-critical path in bounded chunks.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import reviews as _reviews
from .logic_pure import SKILLS
from .reducer import (
    REDUCER_VERSION, build_checkpoint, extend_checkpoint, replay as _replay,
    rules_hash, watermark_tuple,
)


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
    dirty: bool = False


RECENT_WINDOW = 50
CHECKPOINT_SAVE_EVERY = 100
CHECKPOINT_SAVE_INTERVAL_S = 10.0


def loading_projection() -> Dict[str, Any]:
    """Cold-start placeholder: a real projection arrives from the worker's
    first completed pass; UI code must show a quiet updating state."""
    return {
        "loading": True, "revision": -1,
        "xp_micro": {s: 0 for s in SKILLS},
        "levels": {s: 1 for s in SKILLS},
        "total_level": len(SKILLS),
        "inventory": {}, "achievements": [],
        "review_outcomes": {}, "skipped": [],
        "counters": {"successful_actions": 0, "cooking_attempts": 0,
                     "successful_cooks": 0, "gems": 0,
                     "first_catch": 0, "first_cook": 0},
        "per_skill_success": {s: 0 for s in SKILLS},
        "adjustments": [], "diagnostics": [], "conflicts": [],
    }


class EvolvedEngine:
    def __init__(self, config: EngineConfig, journal, *, worker=None):
        self.cfg = config
        self.journal = journal
        self.state = EngineState(lamport=config.lamport_start)
        self._projection_cache: Optional[Dict[str, Any]] = None
        self._checkpoint: Optional[Dict[str, Any]] = None
        self._checkpoint_saved_revision = 0
        self._checkpoint_saved_at = 0.0
        self._worker = worker

    # ------------------------------------------------------------ lifecycle

    def attach_worker(self, worker) -> None:
        """Bind the off-thread projection worker for this loaded game."""
        self._worker = worker
        try:
            worker.set_generation(int(getattr(self, "generation", 0) or 0))
        except Exception:
            pass

    def detach_worker(self):
        worker, self._worker = self._worker, None
        return worker

    # ------------------------------------------------------------ lamport

    def _next_lamport(self) -> int:
        self.state.lamport += 1
        return self.state.lamport

    # -------------------------------------------------------- projections

    def _checkpoint_matches(self, checkpoint: Optional[Dict[str, Any]]) -> bool:
        if not checkpoint:
            return False
        return (checkpoint.get("reducer_version") == REDUCER_VERSION
                and checkpoint.get("rules_hash") == rules_hash(self.cfg.rules)
                and str(checkpoint.get("game_uuid")) == str(self.cfg.game_uuid))

    def invalidate_projection(self) -> None:
        """Drop the cached projection; the worker recomputes (or the next
        synchronous read extends the checkpoint). Never clears the worker's
        last published state: stale data stays visible over blank UI."""
        self._projection_cache = None
        self.state.dirty = True
        if self._worker is not None:
            self._worker.notify_dirty()

    def projection(self) -> Dict[str, Any]:
        """Latest completed projection. Never replays on the calling thread
        when a worker is attached; without a worker it extends incrementally."""
        if self._worker is not None:
            latest = self._worker.latest()
            if latest is not None:
                return latest
            if self._projection_cache is None:
                checkpoint = self._checkpoint or self.journal.load_checkpoint(
                    self.cfg.game_uuid)
                if self._checkpoint_matches(checkpoint):
                    self._checkpoint = checkpoint
                    self._projection_cache = checkpoint.get("state")
            return self._projection_cache or loading_projection()
        if self._projection_cache is None:
            self._recompute_sync()
        return self._projection_cache

    def _recompute_sync(self) -> None:
        checkpoint = self._checkpoint
        if not self._checkpoint_matches(checkpoint):
            loaded = self.journal.load_checkpoint(self.cfg.game_uuid)
            checkpoint = loaded if self._checkpoint_matches(loaded) else None
        if checkpoint:
            watermark = watermark_tuple(checkpoint.get("watermark"))
            if watermark is None and int(checkpoint.get("revision", 0) or 0) == 0:
                watermark = (-1, "", -1, "")
            if watermark is not None and \
                    self.journal.count_operations_through(watermark) == \
                    int(checkpoint.get("revision", 0) or 0):
                extended = extend_checkpoint(
                    checkpoint, self.journal.operations_after(watermark),
                    self.cfg.rules, self.cfg.game_uuid,
                    key_known=self.journal.is_known_review_key_before)
                if extended is not None:
                    self._checkpoint = extended
                    self._projection_cache = extended["state"]
                    self._persist_checkpoint_if_due()
                    return
        built = build_checkpoint(self.journal.all_operations(),
                                 self.cfg.rules, self.cfg.game_uuid)
        self._checkpoint = built
        self._projection_cache = built["state"]
        self._persist_checkpoint_if_due(force=True)

    def _persist_checkpoint_if_due(self, force: bool = False) -> None:
        import time

        checkpoint = self._checkpoint
        if not checkpoint:
            return
        revision = int(checkpoint.get("revision", 0) or 0)
        due = (force
               or revision - self._checkpoint_saved_revision >= CHECKPOINT_SAVE_EVERY
               or time.monotonic() - self._checkpoint_saved_at >= CHECKPOINT_SAVE_INTERVAL_S)
        if not due:
            return
        try:
            self.journal.save_checkpoint(self.cfg.game_uuid, revision, checkpoint)
            self._checkpoint_saved_revision = revision
            self._checkpoint_saved_at = time.monotonic()
        except Exception:
            pass  # derived cache only

    def flush_checkpoint(self) -> None:
        self._persist_checkpoint_if_due(force=True)
        if self._worker is not None:
            self._worker.flush_checkpoint()

    def projection_status(self) -> Dict[str, Any]:
        if self._worker is None:
            return {"loading": self._projection_cache is None, "busy": False,
                    "failed": ""}
        return self._worker.status()

    # -------------------------------------------------------------- credit

    def credit_direct(self, *, revlog_id: int, card_id: int, ease: int,
                      revlog_type: int, review_ts: int, skill: str,
                      resource: str, preview=False, cancelled=False,
                      reward_policy: int = 2) -> Dict[str, Any]:
        """Credit one accepted direct review. Idempotent per review identity.

        reward_policy 2 (new operations): an invalid recipe (missing
        materials or an unmet level) earns zero but still persists and
        observes the review so catch-up can never re-credit it.
        """
        if reward_policy not in (1, 2):
            return {"ok": False, "awarded": False,
                    "error": f"unsupported_reward_policy:{reward_policy}"}
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
            return {"ok": True, "awarded": False, "reason": "duplicate_delivery",
                    "review_key": key}
        before = self.projection()
        before_levels = dict(before.get("levels") or {})
        op = {"op_id": str(uuid.uuid5(uuid.NAMESPACE_URL,
                                      f"{self.cfg.game_uuid}:{key}:direct")),
              "game_uuid": self.cfg.game_uuid, "device_id": self.cfg.device_id,
              "lamport": self._next_lamport(), "kind": "review_award",
              "payload": {"review_key": key, "review_ts": int(review_ts),
                          "rating": int(ease), "review_kind": "review",
                          "provenance": "direct", "reward_policy": int(reward_policy),
                          "skill": str(skill).lower(), "resource": str(resource)}}
        recorded = self.journal.record_review(
            op, review_key=key, revlog_id=revlog_id, card_id=card_id,
            fingerprint=fp)
        if not recorded.get("ok"):
            return {"ok": False, "awarded": False,
                    "error": recorded.get("error", "persist_failed"),
                    "needs_recovery": True, "review_key": key}
        op["device_seq"] = recorded["device_seq"]
        self.state.processed_keys.add(key)
        self._track_recent(key, revlog_id, card_id)
        self.invalidate_projection()
        if self._worker is not None:
            outcome = self._outcome_if_available(key)
            if outcome is None:
                return {"ok": True, "awarded": None, "pending": True,
                        "review_key": key, "device_seq": op["device_seq"],
                        "before_levels": before_levels}
            return self._credit_result(op, key, before_levels, outcome)
        outcome = _reward_outcome(self.projection(), key)
        return self._credit_result(op, key, before_levels, outcome)

    def _outcome_if_available(self, review_key: str) -> Optional[Dict[str, Any]]:
        try:
            projection = self._worker.latest() if self._worker is not None else None
            if not projection:
                return None
            outcomes = projection.get("review_outcomes") or {}
            return dict(outcomes.get(review_key)) if review_key in outcomes else None
        except Exception:
            return None

    def _credit_result(self, op, key, before_levels, outcome) -> Dict[str, Any]:
        projection = self.projection()
        awarded = bool(outcome.get("rewarded"))
        skill = op["payload"]["skill"]
        try:
            level_up = int((projection.get("levels") or {}).get(
                skill, 1)) > int(before_levels.get(skill, 1))
        except Exception:
            level_up = False
        return {"ok": True, "awarded": awarded, "review_key": key,
                "outcome": outcome.get("outcome", ""),
                "paused": outcome.get("paused", ""),
                "level_up": level_up, "projection": projection}

    def outcome_for(self, review_key: str) -> Optional[Dict[str, Any]]:
        """Outcome for one review if the latest completed projection has it.
        Pending outcomes finalize exactly once in the caller."""
        return self._outcome_if_available(review_key) if self._worker is not None \
            else _reward_outcome(self.projection(), review_key)

    def skip_direct(self, *, revlog_id: int, card_id: int, ease: int,
                    revlog_type: int, review_ts: int,
                    reason: str = "classic_mode") -> Dict[str, Any]:
        """Record a durable no-reward claim for a review taken in Classic.

        Syncs as a `review_skip` operation: replay precedence (direct > skip >
        catch-up) prevents any desktop from later catch-up-crediting it.
        """
        eligible = _reviews.is_eligible(
            ease=ease, revlog_type=revlog_type, review_ts=review_ts,
            activated_at=self.cfg.activated_at)
        if not eligible:
            return {"ok": True, "recorded": False, "reason": "ineligible"}
        key = _reviews.make_review_key(self.cfg.game_uuid, revlog_id, card_id)
        if key in self.state.processed_keys:
            return {"ok": True, "recorded": False, "reason": "duplicate"}
        fp = _reviews.immutable_fingerprint(
            revlog_id=revlog_id, card_id=card_id, ease=ease,
            review_ts=review_ts, revlog_type=revlog_type)
        op = {"op_id": str(uuid.uuid5(uuid.NAMESPACE_URL,
                                      f"{self.cfg.game_uuid}:{key}:skip")),
              "game_uuid": self.cfg.game_uuid, "device_id": self.cfg.device_id,
              "lamport": self._next_lamport(), "kind": "review_skip",
              "payload": {"review_key": key, "review_ts": int(review_ts),
                          "reason": str(reason), "provenance": "skip"}}
        recorded = self.journal.record_review(
            op, review_key=key, revlog_id=revlog_id, card_id=card_id,
            fingerprint=fp)
        if not recorded.get("ok"):
            return {"ok": False, "recorded": False,
                    "needs_recovery": True,
                    "error": recorded.get("error", "persist_failed")}
        self.state.processed_keys.add(key)
        self._track_recent(key, revlog_id, card_id)
        self.invalidate_projection()
        return {"ok": True, "recorded": True, "review_key": key}

    def scan_catchup(self, history: List[Dict[str, Any]], *, chunk: int = 500,
                     preset_skill: str = "mining") -> Dict[str, Any]:
        """Chunked catch-up ingestion. Each award persists its operation and
        observation in one transaction; duplicates reuse committed ids."""
        _ = preset_skill
        eligible, stats = _reviews.find_uncredited(history, self.state.processed_keys,
                                                   self.cfg.activated_at)
        made = 0
        for row in eligible[:max(1, chunk)]:
            key = row.get("review_key") or _reviews.make_review_key(
                self.cfg.game_uuid, int(row["revlog_id"]), int(row["card_id"]))
            fp = _reviews.immutable_fingerprint(
                revlog_id=int(row["revlog_id"]), card_id=int(row["card_id"]),
                ease=int(row.get("ease", 3)), review_ts=int(row.get("ts", 0)),
                revlog_type=int(row.get("revlog_type", 1)))
            op = {"op_id": str(uuid.uuid5(uuid.NAMESPACE_URL,
                                          f"{self.cfg.game_uuid}:{key}:catchup")),
                  "game_uuid": self.cfg.game_uuid, "device_id": self.cfg.device_id,
                  "lamport": self._next_lamport(), "kind": "review_award",
                  "payload": {"review_key": key, "review_ts": int(row.get("ts", 0)),
                              "rating": int(row.get("ease", 3)), "review_kind": "review",
                              "provenance": "catchup", "reward_policy": 2}}
            recorded = self.journal.record_review(
                op, review_key=key, revlog_id=int(row["revlog_id"]),
                card_id=int(row["card_id"]), fingerprint=fp)
            if not recorded.get("ok"):
                continue  # conflict/busy: this row reconciles on a later scan
            self.state.processed_keys.add(key)
            self._track_recent(key, int(row["revlog_id"]), int(row["card_id"]))
            made += 1
        if made:
            self.invalidate_projection()
        return {"made": made, "stats": stats}

    def _track_recent(self, key: str, revlog_id: int, card_id: int) -> None:
        self.state.recent[key] = (int(revlog_id), int(card_id))
        while len(self.state.recent) > RECENT_WINDOW:
            self.state.recent.pop(next(iter(self.state.recent)))

    def hydrate(self) -> int:
        """Reload processed identities + recent window from the journal so a
        restart neither re-ingests the same history nor litters duplicate
        catch-up operations. Also advances the lamport clock past history so
        new operations stay in canonical tail order. Returns the observation
        count."""
        try:
            rows = self.journal.review_observations()
        except Exception:
            return 0
        try:
            self.state.lamport = max(self.state.lamport,
                                     int(self.journal.max_lamport()))
        except Exception:
            pass
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
        self.invalidate_projection()
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
            return self.journal.latest_review_disposition(review_key)
        except Exception:
            return "unknown"

    def retract(self, *, review_key: str, reason: str = "anki_undo") -> Dict[str, Any]:
        if self._retract_state(review_key) == "retracted":
            self.state.retracted_for_undo.add(review_key)
            return {"ok": True, "review_key": review_key, "deduped": True}
        op = {"op_id": str(uuid.uuid4()), "game_uuid": self.cfg.game_uuid,
              "device_id": self.cfg.device_id,
              "lamport": self._next_lamport(), "kind": "review_retract",
              "payload": {"target_review_key": review_key, "reason": reason}}
        self.journal.append_operation_auto_seq(op)
        self.invalidate_projection()
        return {"ok": True, "review_key": review_key}

    def restore(self, *, review_key: str, reason: str = "anki_redo") -> Dict[str, Any]:
        if self._retract_state(review_key) == "active":
            self.state.retracted_for_undo.discard(review_key)
            return {"ok": True, "review_key": review_key, "deduped": True}
        op = {"op_id": str(uuid.uuid4()), "game_uuid": self.cfg.game_uuid,
              "device_id": self.cfg.device_id,
              "lamport": self._next_lamport(), "kind": "review_restore",
              "payload": {"target_review_key": review_key, "reason": reason}}
        self.journal.append_operation_auto_seq(op)
        self.invalidate_projection()
        return {"ok": True, "review_key": review_key}

    def _all_ops(self) -> List[Dict[str, Any]]:
        return self.journal.all_operations()


def _reward_outcome(projection: Dict[str, Any], review_key: str) -> Dict[str, Any]:
    """What replay actually granted this review (policy-aware)."""
    outcomes = (projection or {}).get("review_outcomes", {}) or {}
    return dict(outcomes.get(review_key, {}))
