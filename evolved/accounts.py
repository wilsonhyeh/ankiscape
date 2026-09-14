# evolved/accounts.py - Managed account flows over Supabase Auth (Qt-free).
"""Register / verify email code / login / recovery / logout against real local
Auth in tests and production Auth in use. Email codes are entered in the
add-on; there is no reset website. Transport is injectable for unit tests.

Every flow returns an explicit typed outcome instead of a generic failure:
success, verification_required, email_exists, username_taken, invalid_username,
invalid_email, weak_password, invalid_credentials, invalid_code, expired_code,
rate_limited, offline, service_error and cancelled. Raw server detail is
classified against a bounded allowlist and never reaches user copy or
diagnostics; unknown failures stay service_error.

`check_account_status` asks the account-status Edge Function whether an email
is new/unconfirmed/confirmed and whether a proposed username is free, without
creating or emailing anything. Managed signup remains the final authority for
uniqueness; the status check only improves the path taken.

Email verification is required before online game submission (link_game /
submit_operations refuse unverified sessions client-side too).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from .auth import MemorySession, normalize_username, validate_password
from .net import Endpoint, NetError

PostFn = Callable[..., Dict[str, Any]]

# Typed outcomes. Keep this list closed: callers branch on it and unknown
# values would become untranslatable UI states.
OUTCOMES = frozenset({
    "success", "verification_required", "email_exists", "username_taken",
    "invalid_username", "invalid_email", "weak_password",
    "invalid_credentials", "invalid_code", "expired_code", "rate_limited",
    "offline", "service_error", "cancelled",
})

# User-facing copy for each outcome. Never includes server detail.
_COPY = {
    "success": "",
    "verification_required": "Check your email for a verification code.",
    "email_exists": "An account already exists for this email. "
                    "Log in or reset your password.",
    "username_taken": "That username is already taken. Try another.",
    "invalid_username": "Usernames are 3\u201320 characters: a\u2013z, 0\u20139, underscore.",
    "invalid_email": "That email address doesn't look right.",
    "weak_password": "Use at least 6 characters for your password.",
    "invalid_credentials": "Incorrect username or password.",
    "invalid_code": "That code isn't right. Check it and try again.",
    "expired_code": "That code has expired. Request a new one.",
    "rate_limited": "Too many attempts. Wait a moment and try again.",
    "offline": "Couldn't reach the account service. Check your connection "
               "and try again.",
    "service_error": "The account service couldn't complete that. Try again.",
    "cancelled": "Cancelled.",
}

# Bounded allowlist of Auth/PostgREST codes -> typed outcome. Anything not
# listed stays service_error; raw text is never trusted.
_AUTH_CODE_OUTCOMES = {
    "user_already_exists": "email_exists",
    "email_exists": "email_exists",
    "email_taken": "email_exists",
    "user_already_registered": "email_exists",
    "username_taken": "username_taken",
    "invalid_username": "invalid_username",
    "weak_password": "weak_password",
    "password_too_short": "weak_password",
    "password_too_weak": "weak_password",
    "email_address_invalid": "invalid_email",
    "email_invalid": "invalid_email",
    "validation_failed": "invalid_email",
    "invalid_grant": "invalid_credentials",
    "invalid_login_credentials": "invalid_credentials",
    "invalid_password": "invalid_credentials",
    "email_not_confirmed": "verification_required",
    "unverified": "verification_required",
    "otp_expired": "expired_code",
    "token_expired": "expired_code",
    "invalid_token": "invalid_code",
    "otp_invalid": "invalid_code",
    "over_email_send_rate_limit": "rate_limited",
    "over_request_rate_limit": "rate_limited",
    "too_many_requests": "rate_limited",
    "rate_limited": "rate_limited",
}

_OFFLINE_KINDS = ("connection", "timeout", "timed out", "http", "server",
                  "bad_gateway", "gateway_timeout")
_DUPLICATE_KINDS = ("conflict",)

MAX_EMAIL_LEN = 320
MIN_PASSWORD_LEN = 6


@dataclass(frozen=True)
class AccountResult:
    """Typed account-flow outcome. `error` keeps the pre-3.0 field name for
    callers that only render a message; `status` is the machine-readable one."""

    ok: bool
    error: str = ""
    needs_code: bool = False
    session_user_id: str = ""
    status: str = "success"
    retry_after_s: int = 0
    detail: str = ""

    @property
    def cancelled(self) -> bool:
        return self.status == "cancelled"


@dataclass(frozen=True)
class AccountStatus:
    """Result of `check_account_status`. Contacting the service is optional:
    unknown/older servers return status='service_error' with ok False and the
    caller falls back to managed signup."""

    ok: bool
    status: str = "service_error"
    email_status: str = ""            # new | unconfirmed | confirmed
    username_available: bool = True
    retry_after_s: int = 0
    error: str = ""
    detail: str = ""


def _result(status: str, *, ok: bool, needs_code: bool = False,
            session_user_id: str = "", retry_after_s: int = 0,
            detail: str = "", message: str = "") -> AccountResult:
    return AccountResult(ok=ok, error=message or _COPY.get(status, ""),
                         needs_code=needs_code, session_user_id=session_user_id,
                         status=status if status in OUTCOMES else "service_error",
                         retry_after_s=int(retry_after_s or 0),
                         detail=str(detail or "")[:200])


def _error_code(exc: NetError) -> str:
    """Extract a bounded allowlist candidate from a transport error body."""
    code = str(getattr(exc, "code", "") or "").strip()
    if code:
        return code.lower()[:120]
    try:
        data = json.loads(exc.detail)
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    for key in ("error_code", "code", "error", "error_description", "msg",
                "message"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()[:120]
    return ""


def classify_error(exc: NetError, *, context: str = "") -> AccountResult:
    """Map a NetError to an explicit outcome. `context` narrows ambiguous
    codes (e.g. weak_password from Auth on register vs. login)."""
    code = _error_code(exc)
    status = _AUTH_CODE_OUTCOMES.get(code, "")
    if status:
        return _result(status, ok=False, retry_after_s=exc.retry_after or 0,
                       detail=code)
    kind = str(getattr(exc, "kind", "") or "")
    if kind == "rate_limited":
        return _result("rate_limited", ok=False,
                       retry_after_s=exc.retry_after or 0, detail=code)
    if kind == "unauthorized":
        if context == "verify_code":
            return _result("invalid_code", ok=False, detail=code)
        return _result("invalid_credentials", ok=False, detail=code)
    if kind == "forbidden":
        return _result("invalid_credentials" if context in ("login",
                                                            "set_password")
                       else "service_error", ok=False, detail=code)
    if kind in ("invalid", "not_found"):
        # Unknown codes stay service_error: only recognized Auth/PostgREST
        # codes above may claim a precise outcome.
        if context == "verify_code":
            return _result("invalid_code", ok=False, detail=code)
        return _result("service_error", ok=False, detail=code)
    if kind == "conflict":
        return _result("email_exists", ok=False, detail=code)
    if kind in _OFFLINE_KINDS:
        return _result("offline", ok=False, detail=f"{kind}:{code}")
    return _result("service_error", ok=False, detail=f"{kind}:{code}")


def _auth_path(post: PostFn, endpoint: Endpoint, path: str,
               payload: Dict[str, Any],
               token: Optional[str] = None) -> Dict[str, Any]:
    return post(endpoint, path, payload, access_token=token)


def register(post: PostFn, endpoint: Endpoint, *, username: str, email: str,
             password: str) -> AccountResult:
    """Managed signup. A confirmed account never advances to "code sent":
    only an unconfirmed/new account returns verification_required."""
    try:
        norm = normalize_username(username)
    except ValueError:
        return _result("invalid_username", ok=False)
    try:
        validate_password(password)
    except ValueError:
        return _result("weak_password", ok=False)
    if len(password) < MIN_PASSWORD_LEN:
        return _result("weak_password", ok=False)
    email = (email or "").strip()
    if not _valid_email(email):
        return _result("invalid_email", ok=False)
    try:
        data = _auth_path(post, endpoint, "/auth/v1/signup",
                          {"email": email, "password": password,
                           "data": {"username_norm": norm,
                                    "username_display": username.strip()}})
    except NetError as exc:
        return classify_error(exc, context="register")
    if not isinstance(data, dict):
        return _result("service_error", ok=False, detail="malformed_signup")
    user = data.get("user")
    if not isinstance(user, dict):
        # No user object at all: ambiguous success shape (some Auth configs
        # return an empty body for an existing/unconfirmed address). Never
        # claim a code was sent; the caller rechecks status.
        if data.get("access_token"):
            return _result("service_error", ok=False,
                           detail="ambiguous_signup_session_without_user")
        return _result("service_error", ok=False,
                       detail="missing_user_in_signup_response")
    user_id = str(user.get("id", "") or "")
    if data.get("access_token") and data.get("refresh_token") and user_id:
        # Auto-confirm configurations return a live session. The account is
        # usable now, but verification state still decides the outcome.
        if email_verified(user):
            return _result("success", ok=True, session_user_id=user_id)
        return _result("verification_required", ok=True, needs_code=True,
                       session_user_id=user_id)
    if email_verified(user):
        # Confirmed but no session: treat as success of the signup request;
        # the caller signs in with the password.
        return _result("success", ok=True, session_user_id=user_id)
    return _result("verification_required", ok=True, needs_code=True,
                   session_user_id=user_id)


def verify_code(post: PostFn, endpoint: Endpoint, *, email: str, code: str,
                kind: str, session: MemorySession) -> AccountResult:
    """kind: 'signup' or 'recovery'. Stores the managed session memory-only."""
    code = (code or "").strip()
    if not code or len(code) > 32:
        return _result("invalid_code", ok=False)
    try:
        data = _auth_path(post, endpoint, "/auth/v1/verify",
                          {"email": email.strip(), "token": code,
                           "type": kind})
    except NetError as exc:
        return classify_error(exc, context="verify_code")
    return _store_session(data, session)


def login_password(post: PostFn, endpoint: Endpoint, *, email: str,
                   password: str, session: MemorySession) -> AccountResult:
    try:
        validate_password(password)
    except ValueError:
        return _result("invalid_credentials", ok=False)
    try:
        data = _auth_path(post, endpoint, "/auth/v1/token?grant_type=password",
                          {"email": email.strip(), "password": password})
    except NetError as exc:
        return classify_error(exc, context="login")
    return _store_session(data, session)


def login_username(post: PostFn, endpoint: Endpoint, *, username: str,
                   password: str, session: MemorySession) -> AccountResult:
    """Username login via the narrowly scoped Edge Function.

    The function never exposes the stored email. It preserves upstream
    timeouts, 429s and service failures as those outcomes (not wrong-password)
    and reports an unverified account as verification_required so the client
    can ask for the email on the verification page."""
    try:
        norm = normalize_username(username)
        validate_password(password)
    except ValueError:
        return _result("invalid_credentials", ok=False)
    try:
        data = _auth_path(post, endpoint, "/functions/v1/username-login",
                          {"username": norm, "password": password})
    except NetError as exc:
        return classify_error(exc, context="login")
    if isinstance(data, dict) and data.get("error") == "verification_required":
        return _result("verification_required", ok=False,
                       detail="unconfirmed_identity")
    return _store_session(data, session)


def request_recovery(post: PostFn, endpoint: Endpoint, *, email: str) -> AccountResult:
    """Recovery request. Unknown emails read as success (server sends
    nothing); only transport/service failures surface as failures."""
    try:
        _auth_path(post, endpoint, "/auth/v1/recover",
                   {"email": (email or "").strip()})
    except NetError as exc:
        code = _error_code(exc)
        if exc.kind in ("invalid", "not_found") or code in (
                "user_not_found", "email_not_found"):
            return _result("success", ok=True)
        return classify_error(exc, context="recovery")
    return _result("success", ok=True)


def set_new_password(post: PostFn, endpoint: Endpoint, *, access_token: str,
                     new_password: str) -> AccountResult:
    try:
        validate_password(new_password)
    except ValueError:
        return _result("weak_password", ok=False)
    try:
        post(endpoint, "/auth/v1/user", {"password": new_password},
             access_token=access_token, method="PUT")
    except NetError as exc:
        return classify_error(exc, context="set_password")
    return _result("success", ok=True)


def check_account_status(post: PostFn, endpoint: Endpoint, *, email: str,
                         username: str = "") -> AccountStatus:
    """Ask the account-status Edge Function for email state and username
    availability. Creates nothing, sends nothing. An older server (404) is
    reported as service_error so the caller uses managed signup directly."""
    email = (email or "").strip()
    if not _valid_email(email):
        return AccountStatus(False, status="invalid_email",
                             error=_COPY["invalid_email"])
    payload: Dict[str, Any] = {"email": email}
    if username:
        try:
            payload["username"] = normalize_username(username)
        except ValueError:
            return AccountStatus(False, status="invalid_username",
                                 error=_COPY["invalid_username"])
    try:
        data = post(endpoint, "/functions/v1/account-status", payload)
    except NetError as exc:
        if exc.kind == "rate_limited":
            return AccountStatus(False, status="rate_limited",
                                 retry_after_s=exc.retry_after or 0,
                                 error=_COPY["rate_limited"])
        if exc.kind in ("unauthorized", "forbidden", "not_found"):
            # 404: the function is not deployed on this backend. Callers fall
            # back to managed signup rather than pretending to know.
            return AccountStatus(False, status="service_error",
                                 detail="account_status_unavailable",
                                 error=_COPY["service_error"])
        if exc.kind in _OFFLINE_KINDS:
            return AccountStatus(False, status="offline",
                                 error=_COPY["offline"])
        return AccountStatus(False, status="service_error",
                             error=_COPY["service_error"])
    if not isinstance(data, dict):
        return AccountStatus(False, status="service_error",
                             detail="malformed_status",
                             error=_COPY["service_error"])
    email_status = str(data.get("email_status", "") or "")
    if email_status not in ("new", "unconfirmed", "confirmed"):
        return AccountStatus(False, status="service_error",
                             detail="unknown_email_status",
                             error=_COPY["service_error"])
    available = data.get("username_available")
    if not isinstance(available, bool):
        available = True
    return AccountStatus(True, status="success", email_status=email_status,
                         username_available=available)


def logout(session: MemorySession) -> AccountResult:
    """Clear credentials even if the server is unreachable (offline-safe)."""
    session.clear()
    return _result("success", ok=True)


def email_verified(user: Dict[str, Any]) -> bool:
    return bool(user.get("confirmed_at") or user.get("email_confirmed_at"))


def _valid_email(email: str) -> bool:
    return bool(email and "@" in email and len(email) <= MAX_EMAIL_LEN
                and email.strip() == email and " " not in email)


def _store_session(data: Any, session: MemorySession) -> AccountResult:
    try:
        access = data["access_token"]
        refresh = data["refresh_token"]
        user = data.get("user", {}) or {}
        user_id = user.get("id", "")
    except (KeyError, TypeError, AttributeError):
        return _result("service_error", ok=False, detail="malformed_session")
    if not access or not refresh or not user_id:
        return _result("service_error", ok=False, detail="incomplete_session")
    session.set(access_token=access, refresh_token=refresh, user_id=user_id,
                username=(user.get("user_metadata", {}) or {}).get("username_display"))
    if not email_verified(user):
        return _result("verification_required", ok=True, needs_code=True,
                       session_user_id=user_id)
    return _result("success", ok=True, session_user_id=user_id)
