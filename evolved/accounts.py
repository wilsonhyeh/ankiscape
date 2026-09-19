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
    "deleted", "invalid_session", "invalid_confirmation",
    "account_unavailable", "demo_immutable", "delete_unknown",
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
    "deleted": "",
    "invalid_session": "Your sign-in has expired. Sign in again.",
    "invalid_confirmation": "The confirmation didn't match. Check the "
                            "username and password.",
    "account_unavailable": "This account is unavailable right now. Try again "
                           "later or contact support.",
    "demo_immutable": "Demo accounts can't be deleted.",
    "delete_unknown": "Couldn't confirm whether the account was deleted.",
}

# 429 copy. Minutes are only promised when the server supplied a usable
# Retry-After; otherwise the copy stays honest about the unknown wait.
RATE_LIMIT_UNKNOWN_COPY = ("The service is limiting requests right now. "
                           "Try again later.")


def rate_limit_copy(retry_after_s: int = 0, *, email_limit: bool = False) -> str:
    """Honest 429 wording.

    An identified email-send limit carries the email-specific copy at every
    Retry-After value: with a usable wait it promises minute-wise timing,
    without one it names the limit and asks for a spam check. Non-email
    limits keep the generic short/unknown copy. A missing, malformed or
    negative Retry-After never invents a reset time.
    """
    try:
        seconds = int(retry_after_s or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds >= 120:
        minutes = -(-seconds // 60)
        unit = "minute" if minutes == 1 else "minutes"
        if email_limit:
            return (f"Email limit reached \u2014 try again in about "
                    f"{minutes} {unit}. Check spam before requesting "
                    "another code.")
        return f"Too many requests \u2014 try again in about {minutes} {unit}."
    if seconds > 0:
        if email_limit:
            minutes = -(-seconds // 60)
            unit = "minute" if minutes == 1 else "minutes"
            return (f"Email limit reached \u2014 try again in about "
                    f"{minutes} {unit}. Check spam before requesting "
                    "another code.")
        return _COPY["rate_limited"]
    if email_limit:
        return ("Email limit reached. Check spam before requesting another "
                "code, then wait a few minutes and try again.")
    return RATE_LIMIT_UNKNOWN_COPY

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

# Closed codes returned by the account-delete Edge Function.
_DELETE_CODE_OUTCOMES = {
    "invalid_session": "invalid_session",
    "invalid_credentials": "invalid_credentials",
    "invalid_confirmation": "invalid_confirmation",
    "account_unavailable": "account_unavailable",
    "demo_immutable": "demo_immutable",
    "rate_limited": "rate_limited",
    "delete_unknown": "delete_unknown",
    "service_error": "service_error",
    "service_unavailable": "service_error",
}

# Only a connection that never reached the service, or a timeout, is
# "offline". Any HTTP response — including 5xx — is a service answer, so it
# maps to service_error and keeps network wording out of server failures.
_OFFLINE_KINDS = ("connection", "timeout", "timed out")

# Raw server codes are only echoed when they are on this closed list.
_CLOSED_CODES = frozenset(_AUTH_CODE_OUTCOMES) | frozenset(
    _DELETE_CODE_OUTCOMES) | {
    "user_not_found", "email_not_found",
}
_UNKNOWN_AUTH_CODE = "unknown_auth_error"
_UNEXPECTED_EXCEPTION = "unexpected_exception"

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
    if not message and status == "rate_limited":
        message = rate_limit_copy(retry_after_s)
    return AccountResult(ok=ok, error=message or _COPY.get(status, ""),
                         needs_code=needs_code, session_user_id=session_user_id,
                         status=status if status in OUTCOMES else "service_error",
                         retry_after_s=int(retry_after_s or 0),
                         detail=str(detail or "")[:200])


def _error_code(exc: NetError) -> str:
    """Return a closed code label from a transport error.

    The raw body text never travels past this function: a code outside the
    allowlist becomes `unknown_auth_error`, so server text (which may contain
    addresses or other sentinels) can never reach copy or diagnostics.
    """
    code = str(getattr(exc, "code", "") or "").strip()
    raw = ""
    if code:
        raw = code.lower()[:120]
    else:
        try:
            data = json.loads(exc.detail)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            for key in ("error_code", "code", "error", "error_description",
                        "msg", "message"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    raw = value.strip().lower()[:120]
                    break
    if not raw:
        return ""
    return raw if raw in _CLOSED_CODES else _UNKNOWN_AUTH_CODE


def classify_error(exc: NetError, *, context: str = "") -> AccountResult:
    """Map a NetError to an explicit outcome. `context` narrows ambiguous
    codes (e.g. weak_password from Auth on register vs. login)."""
    code = _error_code(exc)
    retry_after = exc.retry_after or 0
    status = _AUTH_CODE_OUTCOMES.get(code, "")
    if status:
        if status == "rate_limited":
            return _result(
                "rate_limited", ok=False, retry_after_s=retry_after,
                detail=code,
                message=rate_limit_copy(
                    retry_after, email_limit=code == "over_email_send_rate_limit"))
        return _result(status, ok=False, retry_after_s=retry_after, detail=code)
    kind = str(getattr(exc, "kind", "") or "")
    if kind == "rate_limited":
        return _result("rate_limited", ok=False, retry_after_s=retry_after,
                       detail=code, message=rate_limit_copy(retry_after))
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
        return _result("email_exists", ok=False,
                       detail="signup_conflict" if context == "register"
                       else code)
    if kind in _OFFLINE_KINDS:
        return _result("offline", ok=False,
                       detail=f"{kind}:{code}" if code else kind)
    return _result("service_error", ok=False,
                   detail=f"{kind}:{code}" if code else kind)


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
    # Real GoTrue returns the user object at the top level when email
    # confirmations are on; other configurations nest it under "user".
    # Accept both, and never throw on a malformed shape.
    if "user" in data:
        user = data.get("user")
    else:
        user = data
    if not isinstance(user, dict):
        return _result("service_error", ok=False,
                       detail="malformed_signup_user")
    user_id = user.get("id")
    if not isinstance(user_id, str) or not user_id.strip():
        return _result("service_error", ok=False,
                       detail="malformed_signup_user")
    user_id = user_id.strip()
    if user.get("identities") == []:
        # Obfuscated existing-user response: user-shaped, no identities.
        # The caller resolves it with one read-only status recheck instead
        # of claiming verification (or success).
        return _result("email_exists", ok=False,
                       detail="obfuscated_duplicate")
    if data.get("access_token") and data.get("refresh_token"):
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


def resend_signup_code(post: PostFn, endpoint: Endpoint, *,
                       email: str) -> AccountResult:
    """Resend the SIGNUP confirmation code via `/auth/v1/resend`.

    Distinct from `request_recovery`, which sends a recovery email: the
    verification path must resend `type=signup`. Unknown emails read as
    accepted, matching recovery's anti-enumeration behavior.
    """
    email = (email or "").strip()
    if not _valid_email(email):
        return _result("invalid_email", ok=False)
    try:
        _auth_path(post, endpoint, "/auth/v1/resend",
                   {"type": "signup", "email": email})
    except NetError as exc:
        code = _error_code(exc)
        if exc.kind in ("invalid", "not_found") or code in (
                "user_not_found", "email_not_found"):
            return _result("success", ok=True)
        return classify_error(exc, context="resend")
    return _result("success", ok=True)


def delete_account(post: PostFn, endpoint: Endpoint, *, access_token: str,
                   username: str, password: str) -> AccountResult:
    """Guarded server-side account deletion.

    Never retried automatically by callers: every outcome is explicit,
    including the ambiguous `delete_unknown`. A 401 is never assumed to mean
    "wrong password"; only the function's closed `invalid_credentials` code
    is reported that way.
    """
    if not str(access_token or "").strip():
        return _result("invalid_session", ok=False)
    if not str(username or "").strip():
        return _result("invalid_confirmation", ok=False)
    password = str(password or "")
    if not password or len(password.encode("utf-8")) > 256:
        return _result("invalid_credentials", ok=False)
    try:
        data = post(endpoint, "/functions/v1/account-delete",
                    {"confirm": "DELETE", "username": username,
                     "password": password},
                    access_token=access_token)
    except NetError as exc:
        code = _error_code(exc)
        mapped = _DELETE_CODE_OUTCOMES.get(code)
        if mapped:
            return _result(mapped, ok=False,
                           retry_after_s=exc.retry_after or 0, detail=code)
        if exc.kind == "rate_limited":
            return _result("rate_limited", ok=False,
                           retry_after_s=exc.retry_after or 0)
        if exc.kind == "unauthorized":
            return _result("invalid_session", ok=False, detail=code)
        if exc.kind in _OFFLINE_KINDS:
            return _result("offline", ok=False, detail=exc.kind)
        if exc.kind in ("invalid", "not_found"):
            return _result("service_error", ok=False, detail=code)
        return _result("service_error", ok=False, detail=code)
    if not isinstance(data, dict) or data.get("deleted") is not True:
        # A malformed 2xx is not proof of deletion.
        return _result("delete_unknown", ok=False, detail="malformed_delete")
    return _result("deleted", ok=True)


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
                                 error=rate_limit_copy(exc.retry_after or 0))
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


def refresh_access_token(post: PostFn, endpoint: Optional[Endpoint],
                         session: Any, *,
                         guard: Optional[Callable[[], bool]] = None) -> bool:
    """Owner-guarded token refresh; the product runtime and the local
    contract harness both call THIS implementation.

    Snapshot (user id + refresh token) is taken before the request. When the
    reply arrives, install or clear happens through the session's own lock
    with the snapshot as an expected value, so a logout or a newer login is
    never undone and the reply can never be applied to a different owner.
    `guard` (when supplied) is re-checked immediately before any mutation:
    an account-operation fence makes the reply stale.
    """
    if endpoint is None or session is None:
        return False
    user_id = getattr(session, "user_id", None)
    refresh_token = getattr(session, "refresh_token", None)
    if not refresh_token:
        return False
    try:
        data = post(endpoint, "/auth/v1/token?grant_type=refresh_token",
                    {"refresh_token": refresh_token})
    except NetError as exc:
        if guard is not None and not guard():
            return False
        if exc.kind in ("unauthorized", "invalid", "forbidden"):
            _guarded_clear(session, refresh_token, user_id)
        return False
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    access, refresh = data.get("access_token"), data.get("refresh_token")
    user = data.get("user", {}) or {}
    returned_id = user.get("id") if isinstance(user, dict) else None
    if not access or not refresh or not isinstance(returned_id, str) \
            or not returned_id:
        return False
    if guard is not None and not guard():
        return False
    if user_id and returned_id != user_id:
        # A different identity came back for this refresh token. Clear the
        # original session; never install the answer.
        _guarded_clear(session, refresh_token, user_id)
        return False
    install = getattr(session, "set_guarded", None)
    if callable(install):
        return bool(install(expect_refresh_token=refresh_token,
                            expect_user_id=user_id,
                            access_token=access, refresh_token=refresh,
                            user_id=returned_id,
                            username=getattr(session, "username", None)))
    if getattr(session, "refresh_token", None) != refresh_token:
        return False
    session.set(access_token=access, refresh_token=refresh,
                user_id=returned_id, username=getattr(session, "username", None))
    return True


def _guarded_clear(session: Any, refresh_token: str,
                   user_id: Optional[str]) -> bool:
    clear = getattr(session, "clear_guarded", None)
    if callable(clear):
        return bool(clear(expect_refresh_token=refresh_token,
                          expect_user_id=user_id))
    if getattr(session, "refresh_token", None) != refresh_token:
        return False
    session.clear()
    return True


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
