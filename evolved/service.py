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
from typing import Any, Callable, Dict, List, Optional, Union

from .net import Endpoint, NetError, post_json
from .sync import UPLOAD_BATCH_MAX, SyncJob, SyncTriggers

PostFn = Callable[..., Union[Dict[str, Any], List[Any]]]

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
        if not isinstance(accepted, list) or not isinstance(conflicts, list):
            # An explicit ack contract: never infer success from a missing or
            # malformed list. The job raises protocol_error on this shape.
            return {"status": "transient", "detail": "missing_ack_list"}
        acked = [str(a) for a in accepted]
        quarantine = [str(c.get("op_id", "")) for c in conflicts
                      if isinstance(c, dict) and c.get("op_id")]
        # `acked` is always present on a well-formed response; the job treats
        # a missing key as a protocol error rather than assumed success.
        return {"status": "ok", "acked": acked, "quarantine": quarantine,
                "rejected": len(quarantine),
                "applied": int(data.get("applied", 0) or 0)}

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
        try:
            next_cursor = int(data.get("next_cursor", cur) or 0)
        except (TypeError, ValueError):
            next_cursor = cur
        # A full page whose cursor advanced means there is likely more; an
        # empty or short page, or a non-advancing cursor, ends the download.
        has_more = len(ops) >= UPLOAD_BATCH_MAX and next_cursor > cur
        return {"status": "ok", "operations": wire_ops,
                "next_cursor": str(next_cursor),
                "has_more": has_more}

    return {"upload": upload, "download": download}


def _session_token(session) -> Optional[str]:
    """Read the live token at request time (never a cached copy)."""
    try:
        return getattr(session, "access_token", None)
    except Exception:
        return None


def _validated_hiscores_row(row: Any) -> Dict[str, Any]:
    """Validate one Hiscores row at the service boundary.

    Typed NetError instead of an uncaught AttributeError/ValueError keeps the
    UI's problem line informative; a half-formed row never reaches Qt.
    """
    if not isinstance(row, dict):
        raise NetError("malformed_response", "hiscores row must be an object")
    username = row.get("username")
    if not isinstance(username, str) or not username.strip():
        raise NetError("malformed_response", "hiscores row missing username")
    xp_raw = row.get("xp")
    if isinstance(xp_raw, bool):
        raise NetError("malformed_response", "hiscores row xp is not numeric")
    try:
        xp = int(xp_raw)
    except (TypeError, ValueError):
        raise NetError("malformed_response", "hiscores row xp is not numeric")
    if xp < 0:
        raise NetError("malformed_response", "hiscores row xp is negative")
    rank_raw = row.get("rank")
    if isinstance(rank_raw, bool):
        raise NetError("malformed_response", "hiscores row rank is not numeric")
    try:
        rank = int(rank_raw)
    except (TypeError, ValueError):
        raise NetError("malformed_response", "hiscores row rank is not numeric")
    if rank < 1:
        raise NetError("malformed_response", "hiscores row rank is not positive")
    is_demo = row.get("is_demo", False)
    if not isinstance(is_demo, bool):
        # Older/newer servers may omit the flag; anything malformed is an
        # explicit false rather than a reason to drop the row.
        is_demo = False
    return {"rank": rank, "username": username, "xp": xp, "is_demo": is_demo}


def _fetch_hiscores_rows(post: PostFn, endpoint: Endpoint, session, *,
                         skill: str, limit: int,
                         cohort: bool = False,
                         public: bool = False) -> List[Dict[str, Any]]:
    """Array-mode Hiscores RPC + per-row validation (shared by Ranks and
    player lookup). Empty list is a successful empty state. `cohort=True`
    selects the authenticated Test leaderboard RPC; the server authorizes it
    from the caller's own player row, never from this flag.

    `public=True` reads the public board with the anonymous key and NO bearer
    token — even when a session exists — so a stale token can never blank the
    public rankings or the request's identity.
    """
    rpc = "test_hiscores" if cohort else "hiscores"
    token = None if public else _session_token(session)
    data = post(endpoint, f"/rest/v1/rpc/{rpc}",
                {"p_skill": str(skill), "p_limit": int(limit)},
                access_token=token, response_shape="array")
    if not isinstance(data, list):
        raise NetError("malformed_response", "hiscores must be a list")
    return [_validated_hiscores_row(row) for row in data]


def fetch_self_context(post: PostFn, endpoint: Endpoint, session) -> Optional[Dict[str, Any]]:
    """Server-reported caller context (username + cohort). None when the
    session is absent, the server is older, or the request fails: callers
    treat unknown as the public cohort and never trust a client flag."""
    if session is None or not _session_token(session):
        return None
    try:
        data = post(endpoint, "/rest/v1/rpc/self_context", {},
                    access_token=_session_token(session))
    except NetError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def query_hiscores(post: PostFn, endpoint: Endpoint, session, *,
                   skill: str, limit: int = 50,
                   cohort: bool = False,
                   public: bool = False) -> List[Dict[str, Any]]:
    """Hiscores query over real transport. Raises NetError on failure; the
    Qt caller converts to a problem line (never a modal). `public=True` uses
    the anonymous public board with no bearer token."""
    from .ui.menu_model import format_hiscores_rows

    return format_hiscores_rows(_fetch_hiscores_rows(
        post, endpoint, session, skill=skill, limit=limit, cohort=cohort,
        public=public))


def query_public_profile(post: PostFn, endpoint: Endpoint, session, *,
                         username: str, skill: str,
                         limit: int = 50,
                         cohort: bool = False,
                         public: bool = False) -> Dict[str, Any]:
    """Player lookup: normalize, fetch the public profile, then match the
    player in the selected-skill Hiscores list for a rank.

    The server's public_profile RPC returns `{username, is_demo, state}`
    (not rank rows). A found player whose rank falls outside the loaded top
    list gets an explicit rank-unavailable row; a missing profile is a
    distinct friendly not-found result (`no_profile`). Unrelated transport
    failures raise NetError so the caller shows a service problem, not "not
    found". `public=True` reads anonymously with no bearer token.
    """
    from .auth import normalize_username
    from .ui.menu_model import format_hiscores_rows, format_xp

    try:
        norm = normalize_username(username)
    except ValueError:
        return {"ok": False, "not_found": True}
    skill = str(skill or "").lower()
    token = None if public else _session_token(session)
    try:
        rpc = "test_public_profile" if cohort else "public_profile"
        data = post(endpoint, f"/rest/v1/rpc/{rpc}",
                    {"p_username_norm": norm},
                    access_token=token)
    except NetError as exc:
        if exc.kind == "not_found" or "no_profile" in str(exc.detail):
            return {"ok": False, "not_found": True}
        raise
    if not isinstance(data, dict):
        raise NetError("malformed_response", "public_profile must be an object")
    name = data.get("username")
    if not isinstance(name, str) or not name.strip():
        raise NetError("malformed_response", "public_profile missing username")
    is_demo = data.get("is_demo", False)
    if not isinstance(is_demo, bool):
        is_demo = False
    state = data.get("state")
    if not isinstance(state, dict) and cohort:
        # Test profile returns the safe allowlist: {username, skills, is_test}.
        skills = data.get("skills")
        if not isinstance(skills, dict):
            raise NetError("malformed_response", "test profile missing skills")
        state = {"xp": {key: (value or {}).get("xp", 0)
                        for key, value in skills.items() if isinstance(value, dict)}}
    if not isinstance(state, dict):
        raise NetError("malformed_response", "public_profile missing state")
    xp_table = state.get("xp")
    if not isinstance(xp_table, dict):
        raise NetError("malformed_response", "public_profile missing xp table")
    xp_raw = xp_table.get(skill, 0)
    if isinstance(xp_raw, bool):
        raise NetError("malformed_response", "public_profile xp is not numeric")
    try:
        xp_micro = int(xp_raw)
    except (TypeError, ValueError):
        raise NetError("malformed_response", "public_profile xp is not numeric")
    if xp_micro < 0:
        raise NetError("malformed_response", "public_profile xp is negative")
    rank: Optional[int] = None
    try:
        for row in _fetch_hiscores_rows(post, endpoint, session,
                                        skill=skill, limit=limit,
                                        cohort=cohort, public=public):
            if row["username"].casefold() == name.casefold():
                rank = row["rank"]
                break
    except NetError:
        rank = None
    row = {"rank": rank, "username": name, "xp": xp_micro,
           "is_demo": is_demo}
    return {"ok": True,
            "profile": {"username": name, "skill": skill, "rank": rank,
                        "xp": xp_micro, "xp_display": format_xp(xp_micro),
                        "is_demo": is_demo},
            "rows": format_hiscores_rows([row]),
            "fetched_at": time.time()}


class SyncService:
    """One SyncJob per game, single-flight, trigger-evaluated, Qt-free."""

    def __init__(self, *, generation: int, game_uuid: str, journal,
                 transport: Dict[str, Callable],
                 apply_remote: Callable[[Dict], None],
                 get_user_id: Callable[[], Optional[str]],
                 triggers: Optional[SyncTriggers] = None,
                 endpoint_project: str = "",
                 initial_last_success: Optional[float] = None):
        self.game_uuid = game_uuid
        self.journal = journal
        self.transport = transport
        self.apply_remote = apply_remote
        self.get_user_id = get_user_id
        self.triggers = triggers or SyncTriggers()
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self.endpoint_project = str(endpoint_project or "")
        if initial_last_success is None:
            try:
                initial_last_success = journal.get_sync_success(
                    self.endpoint_project)
            except Exception:
                initial_last_success = None
        self.job = SyncJob(
            generation=generation, game_uuid=game_uuid,
            user_id=get_user_id(), journal=journal,
            upload=transport["upload"], download=transport["download"],
            apply_remote=apply_remote,
            initial_last_success=initial_last_success,
            endpoint_project=self.endpoint_project)

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
            return {"ok": False, "error": "logged_out_no_network",
                    "status": "logged_out", "pending": self._pending(),
                    "rejected": self._rejected()}
        if self.job.user_id != user_id:
            self.job.user_id = user_id
        if not self.due(**flags):
            return {"ok": False, "error": "not_due", "status": "not_due",
                    "pending": self._pending(), "rejected": self._rejected()}
        try:
            out = self.job.run_once(generation=self.job.generation,
                                    user_id=user_id)
        except RuntimeError as exc:
            # Permanent statuses raise out of run_once: surface them as
            # structured errors with pending preserved (nothing acked).
            return {"ok": False, "error": str(exc)[:300],
                    "status": self.job.state.last_error_kind or "error",
                    "retry_after": self.job.state.retry_after,
                    "pending": self._pending(), "rejected": self._rejected()}
        except Exception as exc:
            return {"ok": False, "error": f"transient:{exc!r}"[:300],
                    "status": self.job.state.last_error_kind or "transient",
                    "retry_after": self.job.state.retry_after,
                    "pending": self._pending(), "rejected": self._rejected()}
        out["pending"] = self._pending()
        out["rejected"] = self._rejected()
        return out

    def force_sync(self) -> Dict[str, Any]:
        """Manual Sync button path: bypass trigger evaluation."""
        return self.maybe_sync(manual=True)

    def _pending(self) -> int:
        try:
            return int(self.journal.count_pending_operations())
        except Exception:
            return -1

    def _rejected(self) -> int:
        try:
            return int(self.journal.count_rejected_operations())
        except Exception:
            return 0

    def backoff_hint_s(self) -> float:
        try:
            return float(self.job.backoff_delay())
        except Exception:
            return 0.0

    def state_token(self) -> str:
        """Typed sync state for the UI. Never string-searches error text at
        the call site; this is the one inference point."""
        try:
            if self.job.state.running:
                return "syncing"
            error = str(self.job.state.last_error or "")
            kind = self.job.state.last_error_kind
            if error:
                if kind == "no_progress" or "no_progress" in error:
                    return "offline"
                if kind == "rate_limited" or "rate_limited" in error:
                    return "offline"
                if "server_update_required" in error:
                    return "server_upgrade"
                if "permanent:unauthorized" in error:
                    return "session_expired"
                if "permanent:forbidden" in error:
                    return "game_mismatch"
                if "permanent:invalid" in error:
                    return "service_error"
                if kind == "protocol_error" or "protocol_error" in error:
                    return "service_error"
                return "offline"
            if self._rejected() > 0:
                return "rejected_progress"
            pending = self._pending()
            if pending > 0:
                return "pending"
            if self.job.state.last_success:
                return "synced"
            return "pending"
        except Exception:
            return "service_error"

    def status_line(self) -> Dict[str, Any]:
        """Render-ready status for settings/Hiscores (no Qt here). Uncapped
        counts only; never a sorted pending scan."""
        return {"logged_in": bool(self.get_user_id()),
                "last_success": self.job.state.last_success,
                "pending": self._pending(),
                "rejected": self._rejected(),
                "state": self.state_token(),
                "last_error": self.job.state.last_error or "",
                "backoff_s": self.backoff_hint_s(),
                "last_activity": time.time()}
