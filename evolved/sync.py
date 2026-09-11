# evolved/sync.py - Durable bidirectional operation sync (contract G/networking).
"""One sync job per game at a time. Persistent outbox survives crashes. On
login: collect new remote ops, merge/replay, upload pending in bounded
batches; repeat until caught up without clearing newer reviews on ack.
Triggers: login, Anki sync completion, 200 new reviews, 20 min activity,
manual Sync button. Time triggers evaluated on events (no idle polling).
Bounded exponential backoff with jitter for transient errors; honor 429
Retry-After. Permanent invalid ops are quarantined with diagnostics, never
retried forever. No network while logged out except explicit auth forms.
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

UPLOAD_BATCH_MAX = 200
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 60.0


@dataclass
class SyncState:
    running: bool = False
    last_success: Optional[float] = None
    pending_count: int = 0
    last_error: Optional[str] = None
    server_cursor: str = ""
    new_reviews_since_sync: int = 0


@dataclass(frozen=True)
class SyncTriggers:
    """Event-driven sync thresholds (contract G): no idle polling.

    Time triggers are evaluated on events only: `min_reviews` counts new
    reviews since the last successful sync, `min_interval_s` counts seconds
    since the last successful sync. The caller tracks pending reviews and
    activity timestamps; `due()` decides whether a sync is owed now.
    """

    min_reviews: int = 200
    min_interval_s: int = 20 * 60

    def due(self, *, new_reviews: int = 0, since_last_success_s: float = 0.0,
            manual: bool = False, on_login: bool = False,
            on_anki_sync: bool = False) -> bool:
        if manual or on_login or on_anki_sync:
            return True
        if new_reviews >= self.min_reviews:
            return True
        return since_last_success_s >= self.min_interval_s


class SyncJob:
    """Single-flight sync bound to (generation, game_uuid, user_id)."""

    def __init__(self, *, generation: int, game_uuid: str, user_id: Optional[str],
                 journal, upload: Callable[[List[Dict]], Dict],
                 download: Callable[[str], Dict],
                 apply_remote: Callable[[Dict], None]):
        self.generation = generation
        self.game_uuid = game_uuid
        self.user_id = user_id
        self.journal = journal
        self._upload = upload
        self._download = download
        self._apply_remote = apply_remote
        self.state = SyncState()
        self._lock = threading.Lock()
        self._failures = 0

    def _fresh(self, generation: int, user_id: Optional[str]) -> bool:
        return (generation == self.generation and (user_id or None) == (self.user_id or None))

    def run_once(self, *, generation: int, user_id: Optional[str]) -> Dict[str, Any]:
        with self._lock:
            if self.state.running:
                return {"ok": False, "error": "already_running"}
            self.state.running = True
        try:
            if generation != self.generation:
                return {"ok": False, "error": "stale_generation_or_user"}
            if (user_id or None) != (self.user_id or None):
                if not user_id:
                    return {"ok": False, "error": "logged_out_no_network"}
                return {"ok": False, "error": "stale_generation_or_user"}
            # 1. Download new remote ops first (merge before upload).
            remote_ops: List[Dict[str, Any]] = []
            cursor = self._current_cursor()
            while True:
                page = self._download(cursor)
                self._raise_for_status(page)
                self._apply_remote(page)
                for op in page.get("operations", []):
                    if isinstance(op, dict):
                        remote_ops.append(op)
                cursor = str(page.get("next_cursor", cursor))
                self._save_cursor(cursor)
                if not page.get("has_more"):
                    break
            self._ingest_remote(remote_ops)
            # 2. Upload pending in bounded batches until caught up.
            while True:
                pending = self.journal.pending_operations(limit=UPLOAD_BATCH_MAX)
                # Never clear newer reviews on ack: snapshot this batch's ids.
                batch_ids = [op["op_id"] for op in pending[:UPLOAD_BATCH_MAX]]
                if not batch_ids:
                    break
                batch = [op for op in pending if op["op_id"] in set(batch_ids)]
                payload = [self._wire_op(op) for op in batch]
                result = self._upload(payload)
                self._raise_for_status(result)
                acked = list(result.get("acked", batch_ids))
                quarantined = list(result.get("quarantine", []))
                if quarantined:
                    self._quarantine(quarantined)
                # Ack only the snapshotted batch ids confirmed by server.
                self.journal.mark_acked([oid for oid in batch_ids if oid in set(acked)])
                if len(batch) < UPLOAD_BATCH_MAX:
                    break
            self.state.last_success = time.time()
            self.state.new_reviews_since_sync = 0
            self.state.last_error = None
            self._failures = 0
            self.state.pending_count = len(self.journal.pending_operations(limit=1))
            return {"ok": True, "cursor": cursor}
        except Exception as exc:
            self._failures += 1
            self.state.last_error = repr(exc)
            raise
        finally:
            with self._lock:
                self.state.running = False

    def note_reviews(self, count: int = 1) -> None:
        """Count freshly credited reviews toward the 200-review trigger."""
        try:
            self.state.new_reviews_since_sync += max(0, int(count))
        except (TypeError, ValueError):
            pass

    def due(self, *, manual: bool = False, on_login: bool = False,
            on_anki_sync: bool = False,
            triggers: Optional[SyncTriggers] = None) -> bool:
        """Event-trigger check (no timers here; caller evaluates on events)."""
        trig = triggers or SyncTriggers()
        since = 0.0
        try:
            if self.state.last_success:
                since = max(0.0, time.time() - self.state.last_success)
            else:
                since = float("inf") if (on_login or manual or on_anki_sync) else 0.0
        except Exception:
            since = 0.0
        return trig.due(new_reviews=self.state.new_reviews_since_sync,
                        since_last_success_s=since, manual=manual,
                        on_login=on_login, on_anki_sync=on_anki_sync)

    def backoff_delay(self) -> float:
        capped = min(BACKOFF_BASE_S * (2 ** min(self._failures, 6)), BACKOFF_MAX_S)
        return capped + random.uniform(0, capped * 0.2)

    def _current_cursor(self) -> str:
        try:
            return str(self.journal.get_server_cursor(self.game_uuid) or "")
        except Exception:
            return self.state.server_cursor

    def _save_cursor(self, cursor: str) -> None:
        self.state.server_cursor = cursor
        try:
            self.journal.set_server_cursor(self.game_uuid, cursor)
        except Exception:
            pass

    def _ingest_remote(self, remote_ops: List[Dict[str, Any]]) -> int:
        """Persist downloaded remote operations into the local journal.

        Uses append-only semantics: exact duplicates are no-ops (same op_id),
        reused IDs with changed content raise from the journal layer and are
        quarantined from re-upload so one poisoned op cannot wedge the outbox.
        Returns the count of newly stored operations.
        """
        from .journal import canonical_json, payload_hash

        stored = 0
        for rop in remote_ops:
            try:
                op_id = str(rop.get("op_id", ""))
                kind = str(rop.get("kind", ""))
                payload = rop.get("payload", {}) or {}
                if not op_id or not kind or not isinstance(payload, dict):
                    continue
                op = {"op_id": op_id,
                      "game_uuid": str(rop.get("game_uuid", self.game_uuid)),
                      "device_id": str(rop.get("device_id", "remote")),
                      "device_seq": int(rop.get("device_seq", 0) or 0),
                      "lamport": int(rop.get("lamport", 0) or 0),
                      "kind": kind, "payload": payload}
            except (TypeError, ValueError, AttributeError):
                continue
            try:
                self.journal.append_operation(op)
                stored += 1
            except Exception:
                # Either an exact duplicate (already have it) or a conflicting
                # ID reuse: confirm which by comparing payload hashes.
                try:
                    have = self.journal.find_operation_payload(op["op_id"])
                    if have is not None and payload_hash(have) != payload_hash(op["payload"]):
                        self._quarantine([op["op_id"]])
                except Exception:
                    pass
                continue
        return stored

    @staticmethod
    def _wire_op(op: Dict[str, Any]) -> Dict[str, Any]:
        """Project a journal row to the submit_operations wire shape.

        The journal row carries storage columns (payload_json text, acked flag,
        payload_hash, created_at); the server accepts op_id/device_id/
        device_seq/lamport/kind/payload only. Never ship storage internals.
        """
        import json as _json

        payload = op.get("payload")
        if payload is None:
            # Journal rows carry payload_json text; wire ops carry payload.
            payload = op.get("payload_json")
        if isinstance(payload, str):
            try:
                payload = _json.loads(payload)
            except ValueError:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return {"op_id": op.get("op_id"), "device_id": op.get("device_id"),
                "device_seq": int(op.get("device_seq", 0) or 0),
                "lamport": int(op.get("lamport", 0) or 0),
                "kind": op.get("kind"), "payload": payload}

    def _quarantine(self, op_ids: List[str]) -> None:
        try:
            self.journal.quarantine(self.game_uuid, op_ids)
        except Exception:
            pass

    @staticmethod
    def _raise_for_status(result: Dict[str, Any]) -> None:
        status = result.get("status", "ok")
        if status == "ok":
            return
        if status == "rate_limited":
            raise RuntimeError(f"rate_limited retry_after={result.get('retry_after')}")
        if status == "upgrade_required":
            raise RuntimeError("server_update_required")
        if status in ("invalid", "conflict", "forbidden", "unauthorized"):
            raise RuntimeError(f"permanent:{status}:{result.get('detail','')}")
        raise RuntimeError(f"transient:{status}:{result.get('detail','')}")
