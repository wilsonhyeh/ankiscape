# evolved/sync.py - Durable bidirectional operation sync (contract G/networking).
"""One sync job per game at a time. Persistent outbox survives crashes.

Download-first merge: each page is validated and committed to the journal
together with its server cursor in ONE SQLite transaction; a malformed or
conflicting page commits nothing and does not advance the cursor. Remote rows
never enter the upload outbox. Work is bounded per turn (2,000 operations or
five seconds of processing between network calls) and returns
{"continue": True} so the caller can reschedule without blocking.

Upload: only snapshotted operation ids are sent; only explicit accepted ids
are acked; explicit conflicts are quarantined with a reason and counted
separately. A missing ack list is a protocol error, never an assumed success.
An unchanged full batch with no ack/quarantine stops with `no_progress`
instead of spinning. "All synced" is only reported when upload-pending and
rejected are both zero after a real pass.
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

UPLOAD_BATCH_MAX = 200
TURN_MAX_OPS = 2000
TURN_MAX_S = 5.0
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 60.0


@dataclass
class SyncState:
    running: bool = False
    last_success: Optional[float] = None
    pending_count: int = 0
    rejected_count: int = 0
    last_error: Optional[str] = None
    server_cursor: str = ""
    new_reviews_since_sync: int = 0
    last_error_kind: str = ""
    retry_after: int = 0


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
                 apply_remote: Callable[[Dict], None],
                 initial_last_success: Optional[float] = None,
                 endpoint_project: str = ""):
        self.generation = generation
        self.game_uuid = game_uuid
        self.user_id = user_id
        self.journal = journal
        self._upload = upload
        self._download = download
        self._apply_remote = apply_remote
        self.endpoint_project = str(endpoint_project or "")
        self.state = SyncState(last_success=initial_last_success)
        self._lock = threading.Lock()
        self._failures = 0

    def _fresh(self, generation: int, user_id: Optional[str]) -> bool:
        return (generation == self.generation and (user_id or None) == (self.user_id or None))

    def run_once(self, *, generation: int, user_id: Optional[str]) -> Dict[str, Any]:
        with self._lock:
            if self.state.running:
                return {"ok": False, "error": "already_running",
                        "status": "already_running"}
            self.state.running = True
        try:
            if generation != self.generation:
                return {"ok": False, "error": "stale_generation_or_user",
                        "status": "stale_generation_or_user"}
            if (user_id or None) != (self.user_id or None):
                if not user_id:
                    return {"ok": False, "error": "logged_out_no_network",
                            "status": "logged_out_no_network"}
                return {"ok": False, "error": "stale_generation_or_user",
                        "status": "stale_generation_or_user"}
            deadline = time.monotonic() + TURN_MAX_S
            # 1. Download new remote ops first (merge before upload), one
            # durable page at a time.
            cursor = self._current_cursor()
            downloaded = 0
            while True:
                if not self._fresh(generation, user_id):
                    return {"ok": False, "error": "stale_generation_or_user",
                            "status": "stale_generation_or_user"}
                page = self._download(cursor)
                self._raise_for_status(page)
                if not self._fresh(generation, user_id):
                    return {"ok": False, "error": "stale_generation_or_user",
                            "status": "stale_generation_or_user"}
                ingested = self.journal.ingest_remote_page(
                    self.game_uuid, page, cursor=str(page.get("next_cursor", "") or ""))
                downloaded += int(ingested.get("stored", 0) or 0)
                has_more = bool(page.get("has_more"))
                cursor = str(ingested.get("cursor", cursor) or cursor)
                self.state.server_cursor = cursor
                try:
                    self._apply_remote(page)
                except Exception:
                    pass
                if not has_more:
                    break
                if downloaded >= TURN_MAX_OPS or time.monotonic() >= deadline:
                    return self._outcome(continue_=True, cursor=cursor)
            # 2. Upload pending in bounded batches until caught up.
            while True:
                if not self._fresh(generation, user_id):
                    return {"ok": False, "error": "stale_generation_or_user",
                            "status": "stale_generation_or_user"}
                pending = self.journal.pending_operations(limit=UPLOAD_BATCH_MAX)
                batch = pending[:UPLOAD_BATCH_MAX]
                batch_ids = [op["op_id"] for op in batch]
                if not batch_ids:
                    break
                payload = [self._wire_op(op) for op in batch]
                result = self._upload(payload)
                self._raise_for_status(result)
                if "acked" not in result:
                    raise RuntimeError("protocol_error:missing_ack_list")
                acked = [str(value) for value in (result.get("acked") or [])]
                quarantined = [str(value) for value in
                               (result.get("quarantine") or [])]
                if quarantined:
                    self._quarantine(quarantined)
                accepted_ids = [oid for oid in batch_ids if oid in set(acked)]
                if accepted_ids:
                    self.journal.mark_acked(accepted_ids)
                if not accepted_ids and not quarantined:
                    # Full batch, nothing accepted, nothing rejected: a server
                    # that silently ignored the batch. Back off, don't spin.
                    raise RuntimeError("no_progress")
                if time.monotonic() >= deadline:
                    return self._outcome(continue_=True, cursor=cursor)
                if len(batch) < UPLOAD_BATCH_MAX:
                    # A short batch means the outbox was drained at snapshot
                    # time. Newer pending ops are picked up by the next pass;
                    # this is not "all synced" (pending count says otherwise).
                    break
            completed_at = time.time()
            self.state.last_success = completed_at
            self.state.new_reviews_since_sync = 0
            self.state.last_error = None
            self.state.last_error_kind = ""
            self._failures = 0
            self._counts()
            self._persist_success(completed_at)
            return self._outcome(ok=True, cursor=cursor)
        except Exception as exc:
            self._failures += 1
            self.state.last_error = repr(exc)
            self.state.last_error_kind = self._error_kind(exc)
            self.state.retry_after = self._retry_after(exc)
            raise
        finally:
            with self._lock:
                self.state.running = False

    @staticmethod
    def _retry_after(exc: Exception) -> int:
        import re as _re
        match = _re.search(r"retry_after=(\d+)", str(exc))
        if not match:
            return 0
        try:
            return max(0, int(match.group(1)))
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------- outcomes
    def _outcome(self, *, ok: bool = False, continue_: bool = False,
                 cursor: str = "") -> Dict[str, Any]:
        self._counts()
        ok = bool(ok or continue_)
        out: Dict[str, Any] = {
            "ok": ok, "cursor": cursor, "continue": bool(continue_),
            "status": "ok" if ok else ("continue" if continue_ else "error"),
            "pending": self.state.pending_count,
            "rejected": self.state.rejected_count,
            "remaining": self.state.pending_count,
        }
        if continue_:
            out["phase"] = "download"
        return out

    def _counts(self) -> None:
        try:
            self.state.pending_count = int(
                self.journal.count_pending_operations())
        except Exception:
            self.state.pending_count = -1
        try:
            self.state.rejected_count = int(
                self.journal.count_rejected_operations())
        except Exception:
            self.state.rejected_count = 0

    def _persist_success(self, ts: float) -> None:
        try:
            self.journal.set_sync_success(self.endpoint_project, ts)
        except Exception:
            pass

    @staticmethod
    def _error_kind(exc: Exception) -> str:
        if type(exc).__name__ == "JournalProtocolError":
            return "protocol_error"
        text = str(exc)
        if text.startswith("no_progress"):
            return "no_progress"
        if text.startswith("protocol_error"):
            return "protocol_error"
        if text.startswith("rate_limited"):
            return "rate_limited"
        if text.startswith("permanent:"):
            return "permanent"
        if "logged_out" in text:
            return "logged_out"
        if "stale_generation" in text:
            return "stale"
        return "transient"

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

    # -------------------------------------------------------------- plumbing
    def _current_cursor(self) -> str:
        try:
            return str(self.journal.get_server_cursor(self.game_uuid) or "")
        except Exception:
            return self.state.server_cursor

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
            self.journal.quarantine(self.game_uuid, op_ids,
                                    reason="server_rejected")
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
