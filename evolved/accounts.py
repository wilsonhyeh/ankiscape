# evolved/accounts.py - Managed account flows over Supabase Auth (Qt-free).
"""Register / verify email code / login / recovery / logout against real local
Auth in tests and production Auth in use. Email codes are entered in the
add-on; there is no reset website. Transport is injectable for unit tests.

Email verification is required before online game submission (link_game /
submit_operations refuse unverified sessions client-side too).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from .auth import MemorySession, normalize_username, validate_password
from .net import Endpoint, NetError

PostFn = Callable[..., Dict[str, Any]]


@dataclass(frozen=True)
class AccountResult:
    ok: bool
    error: str = ""
    needs_code: bool = False
    session_user_id: str = ""


def _auth_path(post: PostFn, endpoint: Endpoint, path: str, payload: Dict[str, Any],
               token: Optional[str] = None) -> Dict[str, Any]:
    return post(endpoint, path, payload, access_token=token)


def register(post: PostFn, endpoint: Endpoint, *, username: str, email: str,
             password: str) -> AccountResult:
    try:
        norm = normalize_username(username)
        validate_password(password)
    except ValueError:
        return AccountResult(False, error="invalid username or password")
    email = (email or "").strip()
    if "@" not in email or len(email) > 320:
        return AccountResult(False, error="invalid username or password")
    try:
        data = _auth_path(post, endpoint, "/auth/v1/signup",
                          {"email": email, "password": password,
                           "data": {"username_norm": norm,
                                    "username_display": username.strip()}})
    except NetError as exc:
        return AccountResult(False, error=_generic(exc))
    if isinstance(data.get("user"), dict) and not data["user"].get("confirmed_at"):
        return AccountResult(True, needs_code=True)
    return AccountResult(True)


def verify_code(post: PostFn, endpoint: Endpoint, *, email: str, code: str,
                kind: str, session: MemorySession) -> AccountResult:
    """kind: 'signup' or 'recovery'. Stores the managed session memory-only."""
    code = (code or "").strip()
    if not code or len(code) > 32:
        return AccountResult(False, error="invalid or expired code")
    try:
        data = _auth_path(post, endpoint, "/auth/v1/verify",
                          {"email": email.strip(), "token": code, "type": kind})
    except NetError as exc:
        return AccountResult(False, error=_generic(exc))
    return _store_session(data, session)


def login_password(post: PostFn, endpoint: Endpoint, *, email: str, password: str,
                   session: MemorySession) -> AccountResult:
    try:
        validate_password(password)
    except ValueError:
        return AccountResult(False, error="invalid username or password")
    try:
        data = _auth_path(post, endpoint, "/auth/v1/token?grant_type=password",
                          {"email": email.strip(), "password": password})
    except NetError as exc:
        return AccountResult(False, error=_generic(exc))
    return _store_session(data, session)


def login_username(post: PostFn, endpoint: Endpoint, *, username: str, password: str,
                   session: MemorySession) -> AccountResult:
    """Username login via the narrowly scoped Edge Function (generic failures)."""
    try:
        norm = normalize_username(username)
        validate_password(password)
    except ValueError:
        return AccountResult(False, error="invalid username or password")
    try:
        data = _auth_path(post, endpoint, "/functions/v1/username-login",
                          {"username": norm, "password": password})
    except NetError as exc:
        return AccountResult(False, error=_generic(exc))
    return _store_session(data, session)


def request_recovery(post: PostFn, endpoint: Endpoint, *, email: str) -> AccountResult:
    try:
        _auth_path(post, endpoint, "/auth/v1/recover", {"email": (email or "").strip()})
    except NetError as exc:
        # Enumeration-resistant: transport errors surface, but an unknown
        # email still reads as success (server sends nothing).
        if exc.kind in ("invalid", "not_found"):
            return AccountResult(True)
        return AccountResult(False, error=_generic(exc))
    return AccountResult(True)


def set_new_password(post: PostFn, endpoint: Endpoint, *, access_token: str,
                     new_password: str) -> AccountResult:
    try:
        validate_password(new_password)
    except ValueError:
        return AccountResult(False, error="invalid username or password")
    try:
        _auth_path(post, endpoint, "/auth/v1/user", {"password": new_password},
                     token=access_token)
    except NetError as exc:
        return AccountResult(False, error=_generic(exc))
    return AccountResult(True)


def logout(session: MemorySession) -> AccountResult:
    """Clear credentials even if the server is unreachable (offline-safe)."""
    session.clear()
    return AccountResult(True)


def email_verified(user: Dict[str, Any]) -> bool:
    return bool(user.get("confirmed_at") or user.get("email_confirmed_at"))


def _store_session(data: Dict[str, Any], session: MemorySession) -> AccountResult:
    try:
        access = data["access_token"]
        refresh = data["refresh_token"]
        user = data.get("user", {}) or {}
        user_id = user.get("id", "")
    except (KeyError, TypeError, AttributeError):
        return AccountResult(False, error="invalid username or password")
    if not access or not refresh or not user_id:
        return AccountResult(False, error="invalid username or password")
    session.set(access_token=access, refresh_token=refresh, user_id=user_id,
                username=(user.get("user_metadata", {}) or {}).get("username_display"))
    if not email_verified(user):
        return AccountResult(True, needs_code=True, session_user_id=user_id)
    return AccountResult(True, session_user_id=user_id)


def _generic(exc: NetError) -> str:
    if exc.kind in ("unauthorized", "forbidden", "invalid", "not_found"):
        return "invalid username or password"
    if exc.kind == "rate_limited":
        return "too many attempts; try again shortly"
    return "network error; try again shortly"
