# evolved/service.py - Sync service wiring (contract G, Qt-free).
"""Binds the SyncJob to real transport so the add-on actually syncs.

Pieces that existed but were never connected:
  - SyncJob (download-first merge, snapshot-ack upload, quarantine) had no
    caller: nothing in the product constructed or ran one.
  - net.post_json knew the wire rules (HTTPS-only, loopback dev exception,
    no redirects, typed NetError) but nothing mapped RPC responses into the
    {status, acked, quarantine, operations, next_cursor, has_more} shape the
    job consumes, or mapped NetError kinds into job statuses.
  - accounts.* flows returned AccountResult/session but nothing enforced
    email-verification before submission, refreshed tokens once on 401, or
    serialized concurrent refresh attempts.
  - SyncTriggers.due() existed with no caller: nobody counted reviews or
    evaluated triggers on login / Anki-sync / manual Sync.

Service owns exactly that glue. It is Qt-free and thread-safe: the product
calls `maybe_sync()` from event hooks (login, sync finish, review credit,
manual button); the service single-flights through one SyncJob per game,
translates transport results, and returns a plain dict the UI can render.
No collection/Qt objects cross into workers: callers pass immutable payloads
and apply journal effects on the main thread via the injected callbacks.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .net import Endpoint, NetError, post_json
from .sync import UPLOAD_BATCH_MAX, SyncJob, SyncTriggers

PostFn = Callable[..., Dict[str, Any]]

# New-protocol submissions carry this header; servers with authoritative
# scoring accept them, older servers can reject with an explicit update
# requirement instead of silently mis-scoring.
CLIENT_PROTOCOL_HEADERS = {"X-AnkiScape-Protocol": "2"}


def _post_tolerant(post: PostFn, endpoint, path, payload, token):
    """Call the transport with protocol headers; tolerate test doubles that
    still implement the older three-argument signature."""
    try:
        return post(endpoint, path, payload, access_token=token,
                    headers=CLIENT_PROTOCOL_HEADERS)
    except TypeError:
        return post(endpoint, path, payload, access_token=token)


@dataclass
class ServiceConfig:
    endpoint: Endpoint
    game_uuid: str
    post: PostFn = post_json


def _map_net_error(exc: NetError) -> Dict[str, Any]:
    """NetError kind -> SyncJob result status (per plan §9 acceptance)."""
    if exc.kind == "rate_limited":
        return {"status": "rate_limited", "retry_after": exc.retry_after or 0,
                "detail": exc.detail}
    if exc.kind in ("invalid", "conflict", "forbidden", "unauthorized",
                    "not_found"):
        return {"status": "invalid" if exc.kind in ("invalid", "not_found")
                else exc.kind, "detail": exc.detail}
    return {"status": "transient", "detail": f"{exc.kind}: {exc.detail}"}


def make_transport(config: ServiceConfig,
                   session) -> Dict[str, Callable]:
    """Build SyncJob upload/download callables over real HTTP transport.

    session: ProfileSession/MemorySession-like (access_token, user_id, and
    either a `refresh()` method or `_refresh_fn`). Refresh is serialized by
    the session; at most one attempt runs across threads. Tokens stay
    memory-only; nothing here writes them to config, journals, or logs.
    """
    endpoint = config.endpoint
    post = config.post
    capability = {"known": False, "ok": True}

    def _do_refresh() -> bool:
        try:
            do_refresh = getattr(session, "refresh", None)
            if callable(do_refresh):
                return bool(do_refresh())
        except Exception:
            pass
        try:
            refresh_fn = getattr(session, "_refresh_fn", None)
            if callable(refresh_fn):
                return bool(refresh_fn(session))
        except Exception:
            pass
        return False

    def _authed(path: str, payload: Dict[str, Any],
                *, _retried: bool = False) -> Dict[str, Any]:
        token = session.access_token
        try:
            return _post_tolerant(post, endpoint, path, payload, token)
        except NetError as exc:
            if exc.kind == "unauthorized" and not _retried:
                if _do_refresh():
                    return _authed(path, payload, _retried=True)
            raise

    def _capabilities_ok() -> bool:
        """False only for a definitively older server (missing RPC)."""
        if capability["known"]:
            return capability["ok"]
        try:
            data = _authed("/rest/v1/rpc/evolved_capabilities", {})
        except NetError as exc:
            if exc.kind == "not_found" or getattr(exc, "status", 0) == 404:
                capability.update(known=True, ok=False)
                return False
            raise
        if not isinstance(data, dict):
            raise NetError("malformed_response", "capabilities must be an object")
        ok = bool(data.get("authoritative_scoring"))
        capability.update(known=True, ok=ok)
        return ok

    def upload(batch: List[Dict[str, Any]]) -> Dict[str, Any]:
        wire = [{"op_id": op.get("op_id"), "device_id": op.get("device_id"),
                 "device_seq": op.get("device_seq"),
                 "lamport": op.get("lamport"), "kind": op.get("kind"),
                 "payload": op.get("payload", {})} for op in batch]
        try:
            if not _capabilities_ok():
                return {"status": "upgrade_required",
                        "detail": "server update required"}
        except NetError as exc:
            return _map_net_error(exc)
        try:
            data = _authed("/rest/v1/rpc/submit_operations",
                           {"p_game_uuid": config.game_uuid, "p_ops": wire})
        except NetError as exc:
            return _map_net_error(exc)
        if not isinstance(data, dict):
            return {"status": "transient", "detail": "malformed response"}
        accepted = data.get("accepted", [])
        conflicts = data.get("conflicts", [])
        acked = [str(a) for a in accepted] if isinstance(accepted, list) else []
        quarantine = [str(c.get("op_id", "")) for c in conflicts
                      if isinstance(c, dict) and c.get("op_id")]
        return {"status": "ok", "acked": acked, "quarantine": quarantine}

    def download(cursor: str) -> Dict[str, Any]:
        try:
            cur = int(cursor) if str(cursor).strip() else 0
        except (TypeError, ValueError):
            cur = 0
        try:
            data = _authed("/rest/v1/rpc/fetch_operations",
                           {"p_game_uuid": config.game_uuid,
                            "p_cursor": cur, "p_limit": UPLOAD_BATCH_MAX})
        except NetError as exc:
            return _map_net_error(exc)
        if not isinstance(data, dict) or not isinstance(
                data.get("operations"), list):
            return {"status": "transient", "detail": "malformed response"}
        ops = data["operations"]
        wire_ops = []
        for row in ops:
            if not isinstance(row, dict):
                continue
            wire_ops.append({
                "op_id": str(row.get("op_id", "")),
                "game_uuid": str(row.get("game_uuid", config.game_uuid)),
                "device_id": str(row.get("device_id", "remote")),
                "device_seq": int(row.get("device_seq", 0) or 0),
                "lamport": int(row.get("lamport", 0) or 0),
                "kind": str(row.get("kind", "")),
                "payload": row.get("payload", {}) or {}})
        has_more = len(ops) >= UPLOAD_BATCH_MAX
        return {"status": "ok", "operations": wire_ops,
                "next_cursor": str(data.get("next_cursor", cur)),
                "has_more": has_more if not data.get("next_cursor") == cur
                else bool(len(ops) >= UPLOAD_BATCH_MAX)}

    return {"upload": upload, "download": download}


def query_hiscores(post: PostFn, endpoint: Endpoint, session, *,
                   skill: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Hiscores query over real transport. Raises NetError on failure; the
    Qt caller converts to a problem line (never a modal)."""
    from .ui.menu_model import format_hiscores_rows

    token = getattr(session, "access_token", None)
    data = post(endpoint, "/rest/v1/rpc/hiscores",
                {"p_skill": str(skill), "p_limit": int(limit)},
                access_token=token)
    if not isinstance(data, list):
        raise NetError("malformed_response", "hiscores must be a list")
    return format_hiscores_rows(data)


class SyncService:
    """One SyncJob per game, single-flight, trigger-evaluated, Qt-free."""

    def __init__(self, *, generation: int, game_uuid: str, journal,
                 transport: Dict[str, Callable],
                 apply_remote: Callable[[Dict], None],
                 get_user_id: Callable[[], Optional[str]],
                 triggers: Optional[SyncTriggers] = None):
        self.game_uuid = game_uuid
        self.journal = journal
        self.transport = transport
        self.apply_remote = apply_remote
        self.get_user_id = get_user_id
        self.triggers = triggers or SyncTriggers()
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self.job = SyncJob(
            generation=generation, game_uuid=game_uuid,
            user_id=get_user_id(), journal=journal,
            upload=transport["upload"], download=transport["download"],
            apply_remote=apply_remote)

    def note_reviews(self, count: int = 1) -> None:
        self.job.note_reviews(count)

    def due(self, **flags) -> bool:
        return self.job.due(**flags, triggers=self.triggers)

    def try_refresh(self, refresh_fn: Callable[[], bool]) -> bool:
        """Serialize concurrent refresh attempts; at most one runs."""
        if not self._refresh_lock.acquire(blocking=False):
            return False
        try:
            return bool(refresh_fn())
        finally:
            self._refresh_lock.release()

    def maybe_sync(self, **flags) -> Dict[str, Any]:
        """Evaluate triggers; run one sync if due. Never raises for
        transport failures: returns {"ok": False, "error": ...} so the UI
        can show pending/offline/problem status instead of a modal."""
        user_id = None
        try:
            user_id = self.get_user_id()
        except Exception:
            user_id = None
        if not user_id:
            return {"ok": False, "error": "logged_out_no_network"}
        if self.job.user_id != user_id:
            self.job.user_id = user_id
        if not self.due(**flags):
            return {"ok": False, "error": "not_due"}
        try:
            out = self.job.run_once(generation=self.job.generation,
                                    user_id=user_id)
        except RuntimeError as exc:
            # Permanent statuses raise out of run_once: surface them as
            # structured errors with pending preserved (nothing acked).
            return {"ok": False, "error": str(exc)[:300],
                    "pending": len(self._pending_ids())}
        except Exception as exc:
            return {"ok": False, "error": f"transient:{exc!r}"[:300],
                    "pending": len(self._pending_ids())}
        out["pending"] = len(self._pending_ids())
        return out

    def force_sync(self) -> Dict[str, Any]:
        """Manual Sync button path: bypass trigger evaluation."""
        return self.maybe_sync(manual=True)

    def _pending_ids(self) -> List[str]:
        try:
            return [op["op_id"]
                    for op in self.journal.pending_operations(limit=1)]
        except Exception:
            return []

    def backoff_hint_s(self) -> float:
        try:
            return float(self.job.backoff_delay())
        except Exception:
            return 0.0

    def status_line(self) -> Dict[str, Any]:
        """Render-ready status for the Hiscores tab (no Qt here)."""
        try:
            pending = len(self.journal.pending_operations(limit=1000))
        except Exception:
            pending = -1
        return {"logged_in": bool(self.get_user_id()),
                "last_success": self.job.state.last_success,
                "pending": pending,
                "last_error": self.job.state.last_error or "",
                "backoff_s": self.backoff_hint_s(),
                "last_activity": time.time()}
