# evolved/link.py - Verified account/game linkage coordinator (Qt-free).
"""One place that links the local game to the signed-in account through the
server's own idempotent `link_game` RPC, before any fetch or upload.

Server rules (0003_registration_gate.sql): the session must be verified, the
account must have a player row, an unclaimed game is bound to the account,
the same game resumes, and anything else raises game_claimed/game_mismatch.
Ownership is never transferred, never silently reassigned and never reset by
this module. Failed linkage does not undo a successful login: callers show
the precise state and keep local progress intact.

The successful binding is persisted locally as non-secret metadata
{game_uuid, user_id, endpoint_project}. It is a convenience guard only; the
server remains the sole authority.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from .net import Endpoint, NetError

LINK_STATES = (
    "linked", "already_linked", "game_claimed", "game_mismatch", "no_profile",
    "unverified", "logged_out", "unconfigured", "offline", "service_error",
)

_STATE_COPY = {
    "linked": "Progress sync is on.",
    "already_linked": "Progress sync is on.",
    "game_claimed": "Signed in; this game belongs to another account.",
    "game_mismatch": "Signed in; this game belongs to another account.",
    "no_profile": "Signed in, but the account profile is missing. "
                  "Recreate the account or contact support.",
    "unverified": "Verify your email before progress can sync.",
    "logged_out": "Not signed in; progress is saved on this computer.",
    "unconfigured": "Online sync is not configured for this build.",
    "offline": "Signed in; sync will start when the connection returns.",
    "service_error": "Signed in; sync setup hit a service problem. "
                     "Try Sync again.",
}

_ERROR_CODE_STATES = {
    "game_claimed": "game_claimed",
    "game_mismatch": "game_mismatch",
    "no_profile": "no_profile",
    "unverified": "unverified",
    "not_authenticated": "logged_out",
    "invalid_game": "service_error",
}


@dataclass(frozen=True)
class LinkResult:
    ok: bool
    state: str
    detail: str = ""
    message: str = ""
    resumed: bool = False

    @property
    def linked(self) -> bool:
        return self.state in ("linked", "already_linked")


def _fail(state: str, detail: str = "") -> LinkResult:
    return LinkResult(False, state, detail=str(detail)[:200],
                      message=_STATE_COPY.get(state, _STATE_COPY["service_error"]))


def _error_code(exc: NetError) -> str:
    code = str(getattr(exc, "code", "") or "").lower()
    if code:
        return code
    try:
        data = json.loads(exc.detail)
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    for key in ("code", "error_code", "message", "msg", "error"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()[:120]
    return ""


def ensure_link(post: Callable[..., Dict[str, Any]], endpoint: Optional[Endpoint],
                session, *, game_uuid: str,
                refresh: Optional[Callable[[], bool]] = None) -> LinkResult:
    """Call the authoritative link_game RPC for the live session.

    Never raises: every failure maps to an explicit actionable state. A 401
    gets ONE refresh attempt (serialized by the caller's session) before it
    is reported as logged_out."""
    if endpoint is None:
        return _fail("unconfigured")
    if not game_uuid:
        return _fail("service_error", "missing_game_uuid")
    token = getattr(session, "access_token", None)
    user_id = getattr(session, "user_id", None)
    if not token or not user_id:
        return _fail("logged_out")
    try:
        data = post(endpoint, "/rest/v1/rpc/link_game",
                    {"p_game_uuid": str(game_uuid)}, access_token=token)
    except NetError as exc:
        if exc.kind == "unauthorized" and callable(refresh):
            try:
                if refresh():
                    return ensure_link(post, endpoint, session,
                                       game_uuid=game_uuid, refresh=None)
            except Exception:
                pass
            return _fail("logged_out", _error_code(exc))
        state = _ERROR_CODE_STATES.get(_error_code(exc), "")
        if state:
            return _fail(state, _error_code(exc))
        if exc.kind in ("connection", "timeout"):
            return _fail("offline", exc.kind)
        if exc.kind in ("invalid", "forbidden", "not_found", "conflict",
                        "unauthorized"):
            # Decode common PostgREST transports of the raise codes.
            detail = _error_code(exc)
            for marker, mapped in _ERROR_CODE_STATES.items():
                if marker in detail:
                    return _fail(mapped, detail)
            if exc.kind == "unauthorized":
                return _fail("logged_out", detail)
            return _fail("service_error", detail or exc.kind)
        if exc.kind == "server":
            return _fail("service_error", "server")
        return _fail("offline", exc.kind)
    resumed = bool(isinstance(data, dict) and data.get("resumed"))
    return LinkResult(True, "already_linked" if resumed else "linked",
                      resumed=resumed,
                      message=_STATE_COPY["already_linked" if resumed
                                          else "linked"])


def binding_metadata(*, game_uuid: str, user_id: str,
                     endpoint_project: str) -> Dict[str, str]:
    """Non-secret local guard. Never credentials, never server authorization."""
    return {"game_uuid": str(game_uuid), "user_id": str(user_id),
            "endpoint_project": str(endpoint_project)}


def persist_binding(journal, result: LinkResult, *, game_uuid: str,
                    user_id: str, endpoint_project: str) -> bool:
    if not result.linked or journal is None:
        return False
    try:
        journal.set_metadata(
            "account_binding",
            json.dumps(binding_metadata(game_uuid=game_uuid, user_id=user_id,
                                        endpoint_project=endpoint_project),
                       sort_keys=True, separators=(",", ":")))
        return True
    except Exception:
        return False


def read_binding(journal) -> Optional[Dict[str, str]]:
    if journal is None:
        return None
    try:
        raw = journal.get_metadata("account_binding")
        if not raw:
            return None
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    game_uuid = str(data.get("game_uuid", "") or "")
    user_id = str(data.get("user_id", "") or "")
    if not game_uuid or not user_id:
        return None
    return {"game_uuid": game_uuid, "user_id": user_id,
            "endpoint_project": str(data.get("endpoint_project", "") or "")}
