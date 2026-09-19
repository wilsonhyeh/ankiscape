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

PAGES = ("login", "register", "verify", "recovery_request", "recovery_confirm",
         "home", "delete")
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
    # Signed-in home + deletion lifecycle (all optional for tests/older hosts).
    home: Callable[[], Dict[str, Any]] = lambda: {}
    on_logout: Callable[[], None] = lambda: None
    on_delete: Callable[..., None] = (
        lambda username, password, delete_local: None)
    on_delete_check: Callable[[], None] = lambda: None
    on_delete_retry_local: Callable[[], None] = lambda: None
    # S13: the register-notice variant ('upgrade' when an offline game exists).
    register_notice_variant: Callable[[], str] = lambda: "first_run"


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
        self._verify_email = ""
        self._recovery_email = ""
        self._recovery_session: Optional[MemorySession] = None
        self._uncertain_registration = False
        self._reset_shortcut = False
        self._password = ""
        self._identity = ""
        self._remember = True
        self._delete_username = ""
        self._delete_local = False
        self._delete_pending = False

    # ------------------------------------------------------------- helpers
    def _emit(self, name: str, **payload) -> None:
        sink = getattr(self.ctx, "emit", None)
        if callable(sink):
            sink(name, payload)

    def _present(self, page: str, *, focus: str = "", status: str = "",
                 **extra) -> None:
        self.page = page
        if status:
            self.status = status
        payload = dict(extra)
        payload.update(page=page, focus=focus, status=self.status,
                       error=self.error)
        self._emit("page", **payload)

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
        if page == "home":
            self.start_home()
            return
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
            "delete": "delete_username",
        }.get(page, "")

    def back(self) -> None:
        """Page back only: never sends a request, keeps nonsensitive inputs."""
        if self.closed or self.busy:
            return
        self.error = ""
        if self.page == "verify":
            self._present("register", focus="password")
        elif self.page == "register":
            email = self._register_fields.get("email", "")
            self._present("login", focus="identity",
                          **({"prefill_identity": email} if email else {}))
        elif self.page == "recovery_confirm":
            self._present("recovery_request", focus="email")
        elif self.page == "recovery_request":
            self._present("login", focus="identity")
        elif self.page == "delete":
            self._present("home")
        else:
            self._present("login", focus="identity")

    def cancel(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._req += 1
        if self._delete_pending:
            # Closing hides the window; the coordinator owns the operation.
            result = {"ok": False, "hiding": True, "deletion_pending": True,
                      "error": "Deletion is in progress."}
        else:
            result = {"ok": False, "cancelled": True, "error": "Cancelled."}
        self._emit("close", result=result)
        self.ctx.on_close(result)

    def can_back(self) -> bool:
        return self.page in ("verify", "register", "recovery_request",
                             "recovery_confirm", "delete") and not self.busy

    def register_notice_variant(self) -> str:
        """S13: the variant the register page renders, from the host's
        operational predicate. Unknown falls back to first-run."""
        fn = getattr(self.ctx, "register_notice_variant", None)
        try:
            return str(fn()) if callable(fn) else "first_run"
        except Exception:
            return "first_run"

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
                if "@" in identity:
                    # Email login: the address is the identity, so the
                    # verification page can use it directly.
                    self._verify_email = identity
                    self._set_status(
                        "This account still needs email verification.")
                else:
                    # Username login never reveals the account email.
                    self._verify_email = ""
                    self._set_status(
                        "This account still needs email verification. Enter "
                        "the email for this account \u2014 we never show it "
                        "from a username login.")
                self._resend_available_at = 0.0
                self._present("verify", focus="code")
                self._emit("resend", available_in=0)
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
        self._reset_shortcut = False
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
                    self._show_email_exists()
                    return
                if status.email_status == "unconfirmed":
                    # Resume verification for that email: never re-register,
                    # never change its username/password.
                    self._verify_email = fields["email"]
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
                self._verify_email = fields["email"]
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
                if result.detail in ("obfuscated_duplicate",
                                     "signup_conflict"):
                    # The server obfuscates duplicates and trigger races
                    # behind user-shaped or 409 answers: one read-only
                    # status probe resolves it, creation is never retried.
                    self._auto_recheck(fields)
                    return
                self._show_email_exists()
                return
            if result.status == "username_taken":
                self._set_error("That username is already taken. Try another.")
                return
            if result.status == "offline":
                # A timeout may still have created the account. Never retry
                # creation automatically; offer Check status.
                self._uncertain_registration = True
                self._set_error(UNKNOWN_COMPLETION_COPY)
                self._emit("uncertain", can_check=True)
                return
            if result.status == "service_error":
                # A malformed 200 or an unknown 5xx (e.g. the username_taken
                # trigger's "Database error saving new user") says nothing
                # definite; one status probe resolves it.
                self._auto_recheck(fields)
                return
            self._set_error(result.error or "Could not create the account.")

        self._run(token, generation, work, apply)

    def _show_email_exists(self) -> None:
        """Definitive duplicate: offer Login and the recovery shortcut."""
        self._set_error("An account already exists for this email. "
                        "Log in or reset your password.")
        self._resume_action = "login"
        self._reset_shortcut = True
        self._emit("reset_shortcut", visible=True)

    def _auto_recheck(self, fields: Dict[str, str]) -> None:
        """Exactly ONE read-only status probe after an ambiguous signup.

        Never retries creation and never recurses. Captured inputs are used
        for the probe; a stale result is discarded by the ordinary
        request/window/generation guards. A failed or rate-limited probe
        keeps the uncertainty visible instead of inventing an outcome.
        """
        token = self._begin()
        if token is None:
            return
        generation = int(self.ctx.generation() or 0)
        captured = dict(fields)
        self._uncertain_registration = True
        self._set_status("Checking whether the account was created\u2026")
        ctx, accounts = self.ctx, self.ctx.accounts

        def work():
            return accounts.check_account_status(
                ctx.post, ctx.endpoint, email=captured.get("email", ""),
                username=captured.get("username", ""))

        def apply(status: AccountStatus) -> None:
            if not status.ok:
                # A failed probe proves nothing: keep the uncertainty.
                self._uncertain_registration = True
                self._set_error(status.error
                                or "Couldn't check status yet. Try again.")
                self._emit("uncertain", can_check=True)
                return
            if status.email_status == "confirmed":
                self._uncertain_registration = False
                self._show_email_exists()
                return
            if status.email_status == "unconfirmed":
                self._uncertain_registration = False
                self._verify_email = captured.get("email", "")
                self._resend_available_at = 0.0
                self._set_status("This email needs verification. Enter the "
                                 "code, or resend one.")
                self._present("verify", focus="code")
                self._emit("resend", available_in=0)
                return
            if not status.username_available:
                self._uncertain_registration = False
                self._set_error("That username is already taken. "
                                "Try another.")
                self._present("register", focus="username")
                return
            self._uncertain_registration = True
            self._set_error(UNKNOWN_COMPLETION_COPY)
            self._emit("uncertain", can_check=True)

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
                # A failed probe is not a conclusion: uncertainty stays.
                self._uncertain_registration = True
                self._set_error(status.error
                                or "Couldn't check status yet. Try again.")
                self._emit("uncertain", can_check=True)
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

    # ------------------------------------------------- signed-in home/delete
    def _home_info(self) -> Dict[str, Any]:
        try:
            info = self.ctx.home()
            return info if isinstance(info, dict) else {}
        except Exception:
            return {}

    def home_info(self) -> Dict[str, Any]:
        info = self._home_info()
        if info.get("logged_in") and not self._delete_username:
            self._delete_username = str(info.get("username", "") or "")
        return info

    def start_home(self) -> None:
        """Signed-in home when a session exists; otherwise the Login page."""
        info = self.home_info()
        if not info.get("logged_in"):
            self._present("login", focus="identity")
            return
        self._present("home")

    def delete_username(self) -> str:
        """The authoritative session display username for the delete gate."""
        if not self._delete_username:
            self._delete_username = str(self.home_info().get("username", "")
                                        or "")
        return self._delete_username

    def open_delete(self) -> None:
        if self.closed or self.busy:
            return
        info = self.home_info()
        if not info.get("logged_in"):
            self._present("login", focus="identity")
            return
        self._delete_username = str(info.get("username", "") or "")
        self.error = ""
        self._present("delete", focus="delete_username")

    def submit_delete(self, password: str, username: str,
                      delete_local: bool) -> None:
        """Client revalidation, then hand off to the deletion coordinator.

        Deletion deliberately does NOT use the flow's request/stale guard:
        the coordinator owns the operation outside this window's lifetime.
        """
        if self.closed or self.busy:
            return
        authoritative = self.delete_username()
        if not authoritative:
            self._set_error("This account can't be deleted right now. "
                            "Try again later.")
            return
        if (username or "") != authoritative:
            self._set_error("Type your username exactly to confirm. "
                            "Nothing was deleted.")
            return
        if not password:
            self._set_error("Enter your password. Nothing was deleted.")
            return
        self._delete_local = bool(delete_local)
        self._delete_pending = True
        self.busy = True
        self._emit("busy", busy=True)
        self._set_error("")
        self._set_status("Deletion is in progress; closing this window will "
                         "not cancel it.")
        self._emit("delete_started", local=self._delete_local)
        try:
            self.ctx.on_delete(username, password, self._delete_local)
        except Exception:
            self._delete_pending = False
            self.busy = False
            self._emit("busy", busy=False)
            self._set_error("Couldn't start the deletion. Nothing was "
                            "deleted.")

    def apply_delete_event(self, event: str, payload: Any = None) -> None:
        """Relay from the coordinator. Safe after the window has closed."""
        if self.closed and event != "deleted":
            return
        payload = payload if isinstance(payload, dict) else {}
        if event == "deleted":
            self._delete_pending = False
            self.busy = False
            self._emit("busy", busy=False)
            self._close_ok(deleted=True)
            return
        if event == "pending":
            return
        if event == "server_deleted":
            self._set_status("Account deleted. Finishing local cleanup\u2026")
            return
        if event == "refused":
            self._delete_pending = False
            self.busy = False
            self._emit("busy", busy=False)
            self._set_error(payload.get("message") or "Nothing was deleted.")
            self._emit("delete_result", status=payload.get("status", "error"))
            return
        if event == "unknown":
            self._delete_pending = False
            self.busy = False
            self._emit("busy", busy=False)
            self._set_error("Could not confirm deletion. Check status "
                            "before trying again.")
            self._emit("delete_result", status="unknown", can_check=True)
            return
        if event == "inconclusive":
            self._set_status("Still checking\u2026")
            self._emit("delete_result", status="unknown", can_check=True)
            return
        if event == "exists":
            self._delete_pending = False
            self.busy = False
            self._emit("busy", busy=False)
            self._set_status("Your account still exists. You can try again.")
            self._emit("delete_result", status="exists")
            return
        if event == "cleanup_incomplete":
            self._delete_pending = False
            self.busy = False
            self._emit("busy", busy=False)
            self._set_error("Account deleted; local cleanup is incomplete. "
                            "Retry the local cleanup below.")
            self._emit("delete_result", status="cleanup_incomplete",
                       can_check=True)

    def check_deletion(self) -> None:
        """Read-only reconciliation. Never sends DELETE."""
        if self.closed:
            return
        self._set_status("Checking\u2026")
        self._emit("delete_result", status="checking")
        try:
            self.ctx.on_delete_check()
        except Exception:
            self._set_error("Couldn't check yet. Try again.")

    def retry_local_cleanup(self) -> None:
        """Local-only retry after a partial cleanup; no server deletion."""
        if self.closed:
            return
        self._set_status("Retrying the local cleanup\u2026")
        try:
            self.ctx.on_delete_retry_local()
        except Exception:
            self._set_error("Couldn't retry the local cleanup yet.")

    def logout(self) -> None:
        """Explicit log out from the home page."""
        try:
            self.ctx.on_logout()
        except Exception:
            pass
        if not self.closed:
            self._delete_username = ""
            self._present("login", focus="identity")

    # -------------------------------------------------------------- verify
    def submit_verify(self, code: str, email: str = "") -> None:
        code = (code or "").strip()
        if email:
            self._verify_email = (email or "").strip()
        if not self._verify_email:
            self._set_error("Enter the email for this account.")
            return
        if not _valid_email(self._verify_email):
            self._set_error("Enter a valid email address.")
            return
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
        email = self._verify_email

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
                # Covers a code that belongs to another email too: keep the
                # generic check-it copy and the transport errors distinct.
                self._set_error("That code isn't right. Check the email "
                                "and code, then try again.")
            else:
                self._set_error(result.error)

        self._run(token, generation, work, apply)

    def resend_code(self, email: str = "") -> None:
        """Explicit user action only. Resends for THIS page: the signup
        confirmation on the verify page, recovery on the reset page. Never
        triggered by Back/redraw/failure, and a recovery recipient is never
        swapped for stale registration fields."""
        page = self.page
        if page == "verify":
            if email:
                self._verify_email = (email or "").strip()
            if not self._verify_email:
                self._set_error("Enter the email for this account.")
                return
            if not _valid_email(self._verify_email):
                self._set_error("Enter a valid email address.")
                return
        elif page == "recovery_confirm":
            if not _valid_email(self._recovery_email):
                self._set_error("Enter your email first.")
                return
        else:
            return
        remaining = self.resend_available_in()
        if remaining > 0:
            self._set_status(f"Wait {remaining}s before resending.")
            return
        token = self._begin()
        if token is None:
            return
        generation = int(self.ctx.generation() or 0)
        ctx, accounts = self.ctx, self.ctx.accounts
        recipient = (self._verify_email if page == "verify"
                     else self._recovery_email)
        self._set_error("")
        self._set_status("Requesting a new code\u2026")

        def work():
            if page == "verify":
                return accounts.resend_signup_code(ctx.post, ctx.endpoint,
                                                   email=recipient)
            return accounts.request_recovery(ctx.post, ctx.endpoint,
                                             email=recipient)

        def apply(result: AccountResult) -> None:
            wait = max(RESEND_MIN_INTERVAL_S, int(result.retry_after_s or 0))
            self._resend_available_at = self.ctx.now() + wait
            if result.ok:
                self._set_status("A new code was requested\u2014check the "
                                 "email for this account; it may take a "
                                 "minute.")
            else:
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
                if result.status == "rate_limited":
                    wait = max(RESEND_MIN_INTERVAL_S,
                               int(result.retry_after_s or 0))
                    self._resend_available_at = self.ctx.now() + wait
                    self._emit("resend", available_in=wait)
                return
            # Conditional wording preserves unknown-email privacy.
            wait = max(RESEND_MIN_INTERVAL_S, int(result.retry_after_s or 0))
            self._set_status("If an account exists for that email, a reset "
                             "code is on its way.")
            self._resend_available_at = self.ctx.now() + wait
            self._emit("resend", available_in=wait)
            self._present("recovery_confirm", focus="code")

        self._run(token, generation, work, apply)

    def open_recovery_with(self, email: str = "") -> None:
        """Reset shortcut: open the recovery request page, prefilled with the
        known email when one is supplied."""
        if self.closed or self.busy:
            return
        email = (email or "").strip()
        self.error = ""
        extra = {"prefill_email": email} if email else {}
        self._present("recovery_request", focus="email", **extra)

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
