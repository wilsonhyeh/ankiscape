# evolved/account_flow.py - One Qt-free controller for the account window.
"""Owns the create/verify/login/recovery page transitions, request lifecycles
and stale-callback guards. UI code (evolved/ui/account.py) only renders pages
and forwards user actions; every network call happens off the UI thread via
the injected `runner`.

Stale work is discarded, never applied: each request captures a token, the
profile generation and the active account identity; a callback whose token is
not current, whose generation changed, or whose window closed is dropped
before it can mutate the session or the UI.

Reset sequence (task 3): verify the recovery code into a TEMPORARY session,
PUT the new password, record the success, install a live session, close the
window and only then hand off to link/sync — which run in the background and
cannot turn a successful password update into a reset failure.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from .accounts import AccountResult, AccountStatus
from .auth import MemorySession, normalize_username, validate_password

PAGES = ("login", "register", "verify", "recovery_request", "recovery_confirm")
RESEND_MIN_INTERVAL_S = 60
UNKNOWN_COMPLETION_COPY = (
    "We couldn't confirm whether the account request completed. "
    "Check status before trying again.")


def _valid_email(email: str) -> bool:
    return bool(email and "@" in email and len(email) <= 320
                and " " not in email.strip() and email == email.strip())


@dataclass
class AccountContext:
    """Everything the flow needs, injected so tests never touch Qt or HTTP."""

    post: Callable[..., Dict[str, Any]]          # net.post_json-compatible
    endpoint: Any
    accounts: Any                                # evolved.accounts module
    profile_session: Any                         # ProfileSession-like
    runner: Callable[[Callable[[], Any], Callable[[Any], None]], None]
    generation: Callable[[], int] = lambda: 0
    user_id: Callable[[], Optional[str]] = lambda: None
    on_close: Callable[[Dict[str, Any]], None] = lambda result: None
    on_verified: Callable[[], None] = lambda: None
    on_password_updated: Callable[[], None] = lambda: None
    on_reset_done: Callable[[], None] = lambda: None
    on_remember: Optional[Callable[[bool], None]] = None
    now: Callable[[], float] = time.monotonic


class AccountFlow:
    """Page machine. All public methods are safe to call from the main thread;
    the work they schedule is delivered back through `runner`."""

    def __init__(self, ctx: AccountContext):
        self.ctx = ctx
        self.page = "login"
        self.busy = False
        self.closed = False
        self.error = ""
        self.status = ""
        self._req = 0
        self._window_token = object()
        self._resend_available_at = 0.0
        self._register_fields = {"username": "", "email": ""}
        self._recovery_email = ""
        self._recovery_session: Optional[MemorySession] = None
        self._uncertain_registration = False
        self._password = ""
        self._identity = ""
        self._remember = True

    # ------------------------------------------------------------- helpers
    def _emit(self, name: str, **payload) -> None:
        sink = getattr(self.ctx, "emit", None)
        if callable(sink):
            sink(name, payload)

    def _present(self, page: str, *, focus: str = "", status: str = "") -> None:
        self.page = page
        if status:
            self.status = status
        self._emit("page", page=page, focus=focus, status=self.status,
                   error=self.error)

    def _set_error(self, message: str) -> None:
        self.error = message
        self._emit("error", message=message)

    def _set_status(self, message: str) -> None:
        self.status = message
        self._emit("status", message=message)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self._emit("busy", busy=busy)

    def _begin(self) -> Optional[int]:
        if self.closed or self.busy:
            return None
        self._req += 1
        self._set_busy(True)
        return self._req

    def _finish(self) -> None:
        self._set_busy(False)

    def _fresh(self, token: int, generation: int) -> bool:
        return (not self.closed and token == self._req
                and int(self.ctx.generation() or 0) == generation)

    def _run(self, token: int, generation: int, work: Callable[[], Any],
             apply_result: Callable[[Any], None]) -> None:
        def _deliver(result: Any) -> None:
            if not self._fresh(token, generation):
                return
            self._finish()
            try:
                apply_result(result)
            except Exception:
                self._set_error("Something went wrong. Try again.")

        def _failed(exc: Exception) -> None:
            if not self._fresh(token, generation):
                return
            self._finish()
            self._set_error("Couldn't reach the account service. Try again.")
            _ = exc

        try:
            self.ctx.runner(work, self._guard(_deliver, _failed))
        except Exception:
            self._finish()
            self._set_error("Couldn't start the request. Try again.")

    def _guard(self, on_done, on_failed):
        window = self._window_token

        def _callback(value):
            if window is not self._window_token:
                return  # a newer window superseded this one
            if isinstance(value, BaseException):
                on_failed(value if isinstance(value, Exception)
                          else Exception(repr(value)))
            else:
                on_done(value)
        return _callback

    # ------------------------------------------------------------ window ops
    def start(self, page: str = "login") -> None:
        self._present(page if page in PAGES else "login",
                      focus=self._focus_for(page))

    @staticmethod
    def _focus_for(page: str) -> str:
        return {
            "login": "identity",
            "register": "username",
            "verify": "code",
            "recovery_request": "email",
            "recovery_confirm": "code",
        }.get(page, "")

    def back(self) -> None:
        """Page back only: never sends a request, keeps nonsensitive inputs."""
        if self.closed or self.busy:
            return
        self.error = ""
        if self.page == "verify":
            self._present("register", focus="password")
        elif self.page == "register":
            self._present("login", focus="identity")
        elif self.page == "recovery_confirm":
            self._present("recovery_request", focus="email")
        elif self.page == "recovery_request":
            self._present("login", focus="identity")
        else:
            self._present("login", focus="identity")

    def cancel(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._req += 1
        result = {"ok": False, "cancelled": True, "error": "Cancelled."}
        self._emit("close", result=result)
        self.ctx.on_close(result)

    def can_back(self) -> bool:
        return self.page in ("verify", "register", "recovery_request",
                             "recovery_confirm") and not self.busy

    def resend_available_in(self) -> int:
        remaining = self._resend_available_at - self.ctx.now()
        return max(0, int(remaining + 0.999))

    # ---------------------------------------------------------------- login
    def submit_login(self, identity: str, password: str,
                     remember: bool = True) -> None:
        identity = (identity or "").strip()
        self._identity = identity
        self._password = password or ""
        self._remember = bool(remember)
        hook = getattr(self.ctx, "on_remember", None)
        if callable(hook):
            try:
                hook(self._remember)
            except Exception:
                pass
        if not identity or not password:
            self._set_error("Enter your username or email and password.")
            return
        token = self._begin()
        if token is None:
            return
        generation = int(self.ctx.generation() or 0)
        self._set_error("")
        self._set_status("Signing in\u2026")
        ctx, accounts = self.ctx, self.ctx.accounts
        sess = ctx.profile_session

        def work():
            temp = MemorySession()
            if "@" in identity:
                result = accounts.login_password(ctx.post, ctx.endpoint,
                                                 email=identity,
                                                 password=password, session=temp)
            else:
                result = accounts.login_username(ctx.post, ctx.endpoint,
                                                 username=identity,
                                                 password=password, session=temp)
            return result, temp

        def apply(pair):
            result, temp = pair
            if result.ok:
                self._install(temp)
                self._close_ok(session_user_id=result.session_user_id)
            elif result.status == "verification_required":
                self._register_fields["email"] = identity
                self._set_status("This account still needs email verification.")
                self._present("verify", focus="code")
            else:
                self._set_error(result.error or "Incorrect username or password.")

        self._run(token, generation, work, apply)

    # ----------------------------------------------------------- registration
    def submit_register(self, username: str, email: str, password: str) -> None:
        self._register_fields = {"username": username or "",
                                 "email": (email or "").strip()}
        self._password = password or ""
        try:
            normalize_username(username or "")
        except ValueError:
            self._set_error("Usernames are 3\u201320 characters: a\u2013z, "
                            "0\u20139, underscore.")
            return
        if not _valid_email(self._register_fields["email"]):
            self._set_error("Enter a valid email address.")
            return
        try:
            validate_password(password or "")
        except ValueError:
            self._set_error("Use at least 6 characters for your password.")
            return
        if len(password or "") < 6:
            self._set_error("Use at least 6 characters for your password.")
            return
        token = self._begin()
        if token is None:
            return
        generation = int(self.ctx.generation() or 0)
        self._set_error("")
        self._uncertain_registration = False
        self._set_status("Creating your account\u2026")
        ctx, accounts = self.ctx, self.ctx.accounts
        fields = dict(self._register_fields)

        def work():
            status = accounts.check_account_status(
                ctx.post, ctx.endpoint, email=fields["email"],
                username=fields["username"])
            return status

        def apply(status: AccountStatus) -> None:
            if status.ok:
                if status.email_status == "confirmed":
                    self._set_error(
                        "An account already exists for this email. "
                        "Log in or reset your password.")
                    self._resume_action = "login"
                    return
                if status.email_status == "unconfirmed":
                    # Resume verification for that email: never re-register,
                    # never change its username/password.
                    self._set_status(
                        "This email needs verification. Enter the code, or "
                        "resend one.")
                    self._resend_available_at = 0.0
                    self._present("verify", focus="code")
                    self._emit("resend", available_in=0)
                    return
                if not status.username_available:
                    self._set_error("That username is already taken. "
                                    "Try another.")
                    return
                self._managed_signup()
                return
            # Status check unavailable (older server, offline): fall back to
            # managed signup directly; keep the typed outcome from Auth.
            self._managed_signup()

        self._run(token, generation, work, apply)

    def _managed_signup(self) -> None:
        ctx, accounts = self.ctx, self.ctx.accounts
        fields = dict(self._register_fields)
        password = self._password
        # The status probe finished; this new request replaces it. Re-begin
        # so the UI is busy again while signup runs and the old token is
        # abandoned (its callback can no longer apply).
        new_token = self._begin()
        if new_token is None:
            return
        token = new_token
        generation = int(self.ctx.generation() or 0)

        def work():
            try:
                return accounts.register(ctx.post, ctx.endpoint,
                                         username=fields["username"],
                                         email=fields["email"],
                                         password=password)
            except Exception as exc:  # never leak the exception to the user
                return AccountResult(False, status="offline", detail=repr(exc))

        def apply(result: AccountResult) -> None:
            if result.status == "verification_required":
                self._set_status("Check your email for a verification code.")
                self._resend_available_at = self.ctx.now() + RESEND_MIN_INTERVAL_S
                self._present("verify", focus="code")
                self._emit("resend", available_in=self.resend_available_in())
                return
            if result.ok:
                if self._password:
                    self.submit_login(fields["email"], self._password,
                                      self._remember)
                else:
                    self._present("login", focus="identity",
                                  status="Account created. Log in.")
                return
            if result.status == "email_exists":
                self._set_error("An account already exists for this email. "
                                "Log in or reset your password.")
                self._resume_action = "login"
                return
            if result.status == "username_taken":
                self._set_error("That username is already taken. Try another.")
                return
            if result.status in ("offline", "service_error"):
                # A timeout may still have created the account. Never retry
                # creation automatically; offer Check status.
                self._uncertain_registration = True
                self._set_error(UNKNOWN_COMPLETION_COPY)
                self._emit("uncertain", can_check=True)
                return
            self._set_error(result.error or "Could not create the account.")

        self._run(token, generation, work, apply)

    def check_status(self) -> None:
        """Explicit Check status action after an uncertain registration."""
        if not self._uncertain_registration:
            return
        fields = dict(self._register_fields)
        token = self._begin()
        if token is None:
            return
        generation = int(self.ctx.generation() or 0)
        self._set_error("")
        self._set_status("Checking\u2026")
        ctx, accounts = self.ctx, self.ctx.accounts

        def work():
            return accounts.check_account_status(
                ctx.post, ctx.endpoint, email=fields["email"],
                username=fields["username"])

        def apply(status: AccountStatus) -> None:
            if not status.ok:
                self._set_error("Couldn't check status yet. Try again.")
                return
            if status.email_status == "confirmed":
                self._uncertain_registration = False
                self._set_status("Your account exists. Log in.")
                self._present("login", focus="identity")
            elif status.email_status == "unconfirmed":
                self._uncertain_registration = False
                self._set_status("Your account needs verification. Enter the "
                                 "code, or resend one.")
                self._present("verify", focus="code")
            else:
                self._uncertain_registration = False
                self._set_error("No account was created. You can try again.")
                self._present("register", focus="username")

        self._run(token, generation, work, apply)

    # -------------------------------------------------------------- verify
    def submit_verify(self, code: str) -> None:
        code = (code or "").strip()
        if not code:
            self._set_error("Enter the code from your email.")
            return
        token = self._begin()
        if token is None:
            return
        generation = int(self.ctx.generation() or 0)
        self._set_error("")
        self._set_status("Verifying\u2026")
        ctx, accounts = self.ctx, self.ctx.accounts
        email = self._register_fields["email"]

        def work():
            temp = MemorySession()
            result = accounts.verify_code(ctx.post, ctx.endpoint, email=email,
                                          code=code, kind="signup",
                                          session=temp)
            return result, temp

        def apply(pair: Any) -> None:
            result, temp = pair
            if result.ok:
                self._install(temp)
                self._close_ok(session_user_id=result.session_user_id)
                self.ctx.on_verified()
                return
            if result.status == "expired_code":
                self._set_error("That code has expired. Resend a new one.")
            elif result.status == "invalid_code":
                self._set_error("That code isn't right. Check it and try "
                                "again.")
            else:
                self._set_error(result.error)

        self._run(token, generation, work, apply)

    def resend_code(self) -> None:
        """Explicit user action only. Never triggered by Back/redraw/failure."""
        remaining = self.resend_available_in()
        if remaining > 0:
            self._set_status(f"Wait {remaining}s before resending.")
            return
        token = self._begin()
        if token is None:
            return
        generation = int(self.ctx.generation() or 0)
        ctx, accounts = self.ctx, self.ctx.accounts
        email = self._register_fields["email"] or self._recovery_email
        if not _valid_email(email):
            self._set_error("Enter your email first.")
            self._finish()
            return
        self._set_error("")
        self._set_status("Requesting a new code\u2026")

        def work():
            return accounts.request_recovery(ctx.post, ctx.endpoint,
                                             email=email)

        def apply(result: AccountResult) -> None:
            if result.ok:
                wait = max(RESEND_MIN_INTERVAL_S, int(result.retry_after_s or 0))
                self._resend_available_at = self.ctx.now() + wait
                self._set_status("A new code was requested. Check your "
                                 "email\u2014it may take a minute.")
                self._emit("resend", available_in=wait)
            else:
                wait = max(1, int(result.retry_after_s or 0))
                self._resend_available_at = self.ctx.now() + wait
                self._set_error(result.error)
                self._emit("resend", available_in=wait)

        self._run(token, generation, work, apply)

    # ------------------------------------------------------------- recovery
    def request_recovery(self, email: str) -> None:
        email = (email or "").strip()
        self._recovery_email = email
        if not _valid_email(email):
            self._set_error("Enter a valid email address.")
            return
        token = self._begin()
        if token is None:
            return
        generation = int(self.ctx.generation() or 0)
        self._set_error("")
        self._set_status("Requesting a reset code\u2026")
        ctx, accounts = self.ctx, self.ctx.accounts

        def work():
            return accounts.request_recovery(ctx.post, ctx.endpoint,
                                             email=email)

        def apply(result: AccountResult) -> None:
            if not result.ok:
                self._set_error(result.error)
                return
            # Conditional wording preserves unknown-email privacy.
            self._set_status("If an account exists for that email, a reset "
                             "code is on its way.")
            self._resend_available_at = self.ctx.now() + RESEND_MIN_INTERVAL_S
            self._present("recovery_confirm", focus="code")

        self._run(token, generation, work, apply)

    def submit_reset(self, code: str, new_password: str) -> None:
        code = (code or "").strip()
        if not code:
            self._set_error("Enter the reset code from your email.")
            return
        if len(new_password or "") < 6:
            self._set_error("Use at least 6 characters for your new password.")
            return
        token = self._begin()
        if token is None:
            return
        generation = int(self.ctx.generation() or 0)
        self._set_error("")
        self._set_status("Updating your password\u2026")
        ctx, accounts = self.ctx, self.ctx.accounts
        email = self._recovery_email
        recovery = self._recovery_session

        def work():
            temp = recovery
            if temp is None or not temp.logged_in:
                temp = MemorySession()
                result = accounts.verify_code(ctx.post, ctx.endpoint,
                                              email=email, code=code,
                                              kind="recovery", session=temp)
                if not result.ok:
                    return {"stage": "verify", "result": result}
            put = accounts.set_new_password(ctx.post, ctx.endpoint,
                                            access_token=temp.access_token,
                                            new_password=new_password)
            return {"stage": "update", "result": put, "session": temp}

        def apply(out: Any) -> None:
            stage = out.get("stage")
            result = out.get("result")
            if stage == "verify":
                if result.status == "expired_code":
                    self._recovery_session = None
                    self._set_error("That code has expired. Request a new one.")
                    self._present("recovery_request", focus="email")
                elif result.status == "invalid_code":
                    self._set_error("That code isn't right. Check it and try "
                                    "again.")
                else:
                    self._set_error(result.error)
                return
            if not result.ok:
                session = out.get("session")
                if session is not None and getattr(session, "logged_in", False):
                    self._recovery_session = session
                if result.status in ("invalid_credentials", "service_error") \
                        and self._recovery_session is not None:
                    # The verified recovery session may have expired: require
                    # a new code rather than reusing it indefinitely.
                    self._recovery_session = None
                    self._set_error("Your reset session expired. Request a "
                                    "new code.")
                    self._present("recovery_request", focus="email")
                else:
                    self._set_error(result.error)
                return
            self._recovery_session = out.get("session")
            # PUT succeeded: record it BEFORE touching persistence. Anything
            # that fails from here on must not ask for another password.
            self._emit("password_updated")
            try:
                self.ctx.on_password_updated()
            except Exception:
                pass
            try:
                self._install(out.get("session"))
            except Exception:
                self._install_failed = True
            self._close_ok(password_updated=True)
            try:
                self.ctx.on_reset_done()
            except Exception:
                pass

        self._run(token, generation, work, apply)

    # -------------------------------------------------------------- common
    def _install(self, memory: Optional[MemorySession]) -> bool:
        if memory is None or not memory.logged_in:
            return False
        profile = self.ctx.profile_session
        try:
            prof_session = getattr(profile, "session", None)
            if prof_session is not None:
                prof_session.set(access_token=memory.access_token,
                                 refresh_token=memory.refresh_token,
                                 user_id=memory.user_id,
                                 username=memory.username)
            elif hasattr(profile, "set"):
                profile.set(access_token=memory.access_token,
                            refresh_token=memory.refresh_token,
                            user_id=memory.user_id, username=memory.username)
            return True
        except Exception:
            # Memory sign-in survives persistence failure.
            return False

    def _close_ok(self, **extra) -> None:
        if self.closed:
            return
        self.closed = True
        self._req += 1
        result = {"ok": True, **extra}
        self._emit("close", result=result)
        self.ctx.on_close(result)
