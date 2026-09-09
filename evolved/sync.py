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
            cursor = self._current_cursor()
            while True:
                page = self._download(cursor)
                self._raise_for_status(page)
                self._apply_remote(page)
                cursor = str(page.get("next_cursor", cursor))
                self._save_cursor(cursor)
                if not page.get("has_more"):
                    break
            # 2. Upload pending in bounded batches until caught up.
            while True:
                pending = self.journal.pending_operations(limit=UPLOAD_BATCH_MAX)
                # Never clear newer reviews on ack: snapshot this batch's ids.
                batch_ids = [op["op_id"] for op in pending[:UPLOAD_BATCH_MAX]]
                if not batch_ids:
                    break
                batch = [op for op in pending if op["op_id"] in set(batch_ids)]
                result = self._upload(batch)
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
        if status in ("invalid", "conflict", "forbidden", "unauthorized"):
            raise RuntimeError(f"permanent:{status}:{result.get('detail','')}")
        raise RuntimeError(f"transient:{status}:{result.get('detail','')}")
