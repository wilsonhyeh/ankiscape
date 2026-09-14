import json
import unittest

from evolved.account_flow import AccountContext, AccountFlow
from evolved.accounts import AccountResult, AccountStatus
from evolved.auth import MemorySession


class _FakeAccounts:
    """Scripted account module: each call pops the next scripted result."""

    def __init__(self):
        self.script = {}
        self.calls = []

    def push(self, name, value):
        self.script.setdefault(name, []).append(value)

    def _next(self, name):
        values = self.script.get(name) or []
        if values:
            return values.pop(0)
        return AccountResult(False, "no script")

    def check_account_status(self, post, endpoint, *, email, username=""):
        self.calls.append(("status", email, username))
        return self._next("status")

    def register(self, post, endpoint, *, username, email, password):
        self.calls.append(("register", username, email, password))
        return self._next("register")

    def verify_code(self, post, endpoint, *, email, code, kind, session):
        self.calls.append(("verify", email, code, kind))
        result = self._next("verify")
        if result.ok:
            session.set(access_token="tok", refresh_token="ref",
                        user_id="u-1", username="wilson")
        return result

    def login_password(self, post, endpoint, *, email, password, session):
        self.calls.append(("login_password", email, password))
        result = self._next("login")
        if result.ok:
            session.set(access_token="tok", refresh_token="ref",
                        user_id="u-1", username="wilson")
        return result

    def login_username(self, post, endpoint, *, username, password, session):
        self.calls.append(("login_username", username, password))
        result = self._next("login")
        if result.ok:
            session.set(access_token="tok", refresh_token="ref",
                        user_id="u-1", username="wilson")
        return result

    def request_recovery(self, post, endpoint, *, email):
        self.calls.append(("recovery", email))
        return self._next("recovery")

    def resend_signup_code(self, post, endpoint, *, email):
        self.calls.append(("resend_signup", email))
        return self._next("resend_signup")

    def set_new_password(self, post, endpoint, *, access_token, new_password):
        self.calls.append(("set_password", access_token, new_password))
        return self._next("set_password")


class _ProfileSession:
    def __init__(self, *, install_raises=False):
        self.session = MemorySession()
        self.vault = None
        self.remember = False
        self.install_raises = install_raises

    @property
    def logged_in(self):
        return self.session.logged_in

    @property
    def user_id(self):
        return self.session.user_id


class _Runner:
    """Synchronous runner; `defer=True` holds callbacks for staleness tests."""

    def __init__(self, defer: bool = False):
        self.deferred = [] if defer else None

    def __call__(self, work, callback):
        try:
            value = work()
        except Exception as exc:
            value = exc
        if self.deferred is not None:
            self.deferred.append((callback, value))
        else:
            callback(value)

    def deliver_all(self):
        for callback, value in self.deferred:
            callback(value)
        self.deferred = []


def _flow(*, runner=None, install_raises=False, home=None, on_delete=None,
          on_delete_check=None, on_delete_retry_local=None):
    events = []
    accounts = _FakeAccounts()
    profile = _ProfileSession()
    if install_raises:
        def _boom(_session):
            raise RuntimeError("vault write failed")
        profile.session.on_change = _boom
    ctx = AccountContext(
        post=lambda *a, **k: {},
        endpoint=object(),
        accounts=accounts,
        profile_session=profile,
        runner=runner or _Runner(),
        generation=lambda: 1,
        user_id=lambda: profile.user_id,
        on_close=lambda result: events.append(("flow_closed", result)),
        on_verified=lambda: events.append(("verified", {})),
        on_password_updated=lambda: events.append(("password_updated", {})),
        on_reset_done=lambda: events.append(("reset_done", {})),
        home=home or (lambda: {}),
        on_delete=on_delete or (lambda u, p, l: events.append(
            ("on_delete", {"username": u, "password": p, "local": l}))),
        on_delete_check=on_delete_check or (lambda: events.append(
            ("on_delete_check", {}))),
        on_delete_retry_local=on_delete_retry_local or (lambda: events.append(
            ("on_delete_retry_local", {}))),
    )
    ctx.emit = lambda name, payload: events.append((name, payload))
    flow = AccountFlow(ctx)
    return flow, accounts, events


class TestRegistrationOutcomes(unittest.TestCase):
    def test_confirmed_email_stops_before_signup(self):
        flow, accounts, events = _flow()
        accounts.push("status", AccountStatus(True, email_status="confirmed"))
        flow.start("register")
        flow.submit_register("wilson", "w@example.com", "pw123456")
        self.assertEqual(accounts.calls, [("status", "w@example.com", "wilson")])
        self.assertNotIn(("register", "wilson", "w@example.com", "pw123456"),
                         accounts.calls)
        errors = [e for e in events if e[0] == "error"]
        self.assertIn("already exists", errors[-1][1]["message"])

    def test_unconfirmed_email_resumes_verification_without_signup(self):
        flow, accounts, events = _flow()
        accounts.push("status", AccountStatus(True, email_status="unconfirmed"))
        flow.start("register")
        flow.submit_register("wilson", "w@example.com", "pw123456")
        pages = [e[1]["page"] for e in events if e[0] == "page"]
        self.assertEqual(pages[-1], "verify")
        self.assertFalse(any(call[0] == "register" for call in accounts.calls))

    def test_new_email_seeds_and_requires_verification(self):
        flow, accounts, events = _flow()
        accounts.push("status", AccountStatus(True, email_status="new",
                                              username_available=True))
        accounts.push("register", AccountResult(
            True, status="verification_required", needs_code=True))
        flow.start("register")
        flow.submit_register("wilson", "w@example.com", "pw123456")
        pages = [e[1]["page"] for e in events if e[0] == "page"]
        self.assertEqual(pages[-1], "verify")

    def test_username_taken_keeps_fields_and_stays(self):
        flow, accounts, events = _flow()
        accounts.push("status", AccountStatus(True, email_status="new",
                                              username_available=False))
        flow.start("register")
        flow.submit_register("wilson", "w@example.com", "pw123456")
        pages = [e[1]["page"] for e in events if e[0] == "page"]
        self.assertEqual(pages[-1], "register")
        self.assertEqual(flow._register_fields["username"], "wilson")
        self.assertFalse(any(call[0] == "register" for call in accounts.calls))

    def test_uncertain_registration_offers_check_status(self):
        flow, accounts, events = _flow()
        accounts.push("status", AccountStatus(False, status="offline"))
        accounts.push("register", AccountResult(False, status="offline"))
        flow.start("register")
        flow.submit_register("wilson", "w@example.com", "pw123456")
        self.assertTrue(flow._uncertain_registration)
        self.assertTrue(any(e[0] == "uncertain" for e in events))
        accounts.push("status", AccountStatus(True, email_status="confirmed"))
        flow.check_status()
        pages = [e[1]["page"] for e in events if e[0] == "page"]
        self.assertEqual(pages[-1], "login")
        self.assertFalse(flow._uncertain_registration)


class TestVerificationAndLogin(unittest.TestCase):
    def test_verify_installs_session_and_closes(self):
        flow, accounts, events = _flow()
        accounts.push("status", AccountStatus(True, email_status="new"))
        accounts.push("register", AccountResult(
            True, status="verification_required", needs_code=True))
        accounts.push("verify", AccountResult(True))
        flow.start("register")
        flow.submit_register("wilson", "w@example.com", "pw123456")
        flow.submit_verify("123456")
        self.assertTrue(flow.ctx.profile_session.logged_in)
        closes = [e for e in events if e[0] == "close"]
        self.assertTrue(closes and closes[-1][1]["result"]["ok"])
        self.assertTrue(any(e[0] == "verified" for e in events))

    def test_unverified_login_goes_to_verify_page(self):
        flow, accounts, events = _flow()
        accounts.push("login", AccountResult(
            False, status="verification_required"))
        flow.start("login")
        flow.submit_login("wilson", "pw123456")
        pages = [e[1]["page"] for e in events if e[0] == "page"]
        self.assertEqual(pages[-1], "verify")

    def test_stale_callback_is_discarded(self):
        runner = _Runner(defer=True)
        flow, accounts, events = _flow(runner=runner)
        accounts.push("login", AccountResult(True))
        flow.start("login")
        flow.submit_login("wilson", "pw123456")
        # Generation changed before delivery: the session must not install.
        flow.ctx.generation = lambda: 2
        runner.deliver_all()
        self.assertFalse(flow.ctx.profile_session.logged_in)
        self.assertFalse(any(e[0] == "close" for e in events))


class TestRecoveryAndReset(unittest.TestCase):
    def _to_confirm(self, flow, accounts):
        accounts.push("recovery", AccountResult(True))
        flow.start("login")
        flow.request_recovery("w@example.com")
        return flow

    def test_reset_put_success_with_broken_persistence_still_succeeds(self):
        flow, accounts, events = _flow(install_raises=True)
        self._to_confirm(flow, accounts)
        accounts.push("verify", AccountResult(True))
        accounts.push("set_password", AccountResult(True))
        flow.submit_reset("123456", "newpw123")
        self.assertTrue(any(e[0] == "password_updated" for e in events))
        closes = [e for e in events if e[0] == "close"]
        self.assertTrue(closes and closes[-1][1]["result"]["ok"])
        # No reset page error was shown after the successful PUT.
        errors = [e for e in events if e[0] == "error"
                  and e[1].get("message")]
        self.assertFalse(any("password" in e[1]["message"].lower()
                             for e in errors))

    def test_reset_with_expired_recovery_session_requests_new_code(self):
        flow, accounts, events = _flow()
        self._to_confirm(flow, accounts)
        accounts.push("verify", AccountResult(True))
        accounts.push("set_password",
                      AccountResult(False, status="invalid_credentials"))
        flow.submit_reset("123456", "newpw123")
        pages = [e[1]["page"] for e in events if e[0] == "page"]
        self.assertEqual(pages[-1], "recovery_request")
        self.assertIsNone(flow._recovery_session)

    def test_expired_code_returns_to_request_page(self):
        flow, accounts, events = _flow()
        self._to_confirm(flow, accounts)
        accounts.push("verify", AccountResult(False, status="expired_code"))
        flow.submit_reset("999999", "newpw123")
        pages = [e[1]["page"] for e in events if e[0] == "page"]
        self.assertEqual(pages[-1], "recovery_request")

    def test_put_failure_keeps_confirm_page(self):
        flow, accounts, events = _flow()
        self._to_confirm(flow, accounts)
        accounts.push("verify", AccountResult(True))
        accounts.push("set_password",
                      AccountResult(False, status="weak_password"))
        flow.submit_reset("123456", "newpw123")
        pages = [e[1]["page"] for e in events if e[0] == "page"]
        self.assertEqual(pages[-1], "recovery_confirm")
        self.assertFalse(any(e[0] == "password_updated" for e in events))


class TestResendCooldown(unittest.TestCase):
    def test_resend_is_explicit_and_cooldown_gated(self):
        now = {"t": 1000.0}
        flow, accounts, events = _flow()
        flow.ctx.now = lambda: now["t"]
        accounts.push("status", AccountStatus(True, email_status="new"))
        accounts.push("register", AccountResult(
            True, status="verification_required", needs_code=True))
        flow.start("register")
        flow.submit_register("wilson", "w@example.com", "pw123456")
        self.assertGreater(flow.resend_available_in(), 0)
        before = len(accounts.calls)
        flow.resend_code()  # gated: no request
        self.assertEqual(len(accounts.calls), before)
        now["t"] += 61
        accounts.push("resend_signup", AccountResult(True))
        flow.resend_code()
        # The verify page resends SIGNUP, never recovery.
        self.assertEqual(accounts.calls[-1][0], "resend_signup")
        # Recovery pages still route to recovery, with only the recovery
        # recipient (never the stale registration email).
        flow.start("recovery_request")
        flow.request_recovery("other@example.com")
        now["t"] += 61
        accounts.push("recovery", AccountResult(True))
        flow.resend_code()
        self.assertEqual(accounts.calls[-1][0], "recovery")
        self.assertEqual(accounts.calls[-1][1], "other@example.com")


class TestVerifyEmailField(unittest.TestCase):
    def test_username_login_leaves_email_empty_and_requires_input(self):
        flow, accounts, events = _flow()
        accounts.push("login", AccountResult(
            False, status="verification_required"))
        flow.start("login")
        flow.submit_login("wilson", "pw123456")
        self.assertEqual(flow._verify_email, "")
        self.assertEqual(flow.page, "verify")
        statuses = [e[1]["message"] for e in events if e[0] == "status"]
        self.assertTrue(any("never show it" in m for m in statuses))
        before = len(accounts.calls)
        flow.submit_verify("123456")
        self.assertEqual(len(accounts.calls), before)
        errors = [e[1]["message"] for e in events if e[0] == "error"]
        self.assertIn("Enter the email for this account.", errors)

    def test_email_login_prefills_and_verifies(self):
        flow, accounts, events = _flow()
        accounts.push("login", AccountResult(
            False, status="verification_required"))
        flow.start("login")
        flow.submit_login("w@example.com", "pw123456")
        self.assertEqual(flow._verify_email, "w@example.com")
        accounts.push("verify", AccountResult(True))
        flow.submit_verify("123456")
        self.assertEqual(accounts.calls[-1],
                         ("verify", "w@example.com", "123456", "signup"))

    def test_submit_verify_with_typed_email(self):
        flow, accounts, events = _flow()
        flow.start("verify")
        accounts.push("verify", AccountResult(True))
        flow.submit_verify("123456", "typed@example.com")
        self.assertEqual(flow._verify_email, "typed@example.com")
        self.assertEqual(accounts.calls[-1],
                         ("verify", "typed@example.com", "123456", "signup"))

    def test_resend_uses_edited_verify_email(self):
        flow, accounts, events = _flow()
        accounts.push("status", AccountStatus(True, email_status="unconfirmed"))
        flow.start("register")
        flow.submit_register("wilson", "w@example.com", "pw123456")
        self.assertEqual(flow.page, "verify")
        accounts.push("resend_signup", AccountResult(True))
        flow.resend_code("edited@example.com")
        self.assertEqual(accounts.calls[-1],
                         ("resend_signup", "edited@example.com"))
        self.assertIn("edited@example.com", flow._verify_email)

    def test_recovery_resend_only_uses_recovery_recipient(self):
        now = {"t": 1000.0}
        flow, accounts, events = _flow()
        flow.ctx.now = lambda: now["t"]
        flow._register_fields = {"username": "wilson", "email": "a@x.com"}
        flow.start("recovery_request")
        accounts.push("recovery", AccountResult(True))
        flow.request_recovery("b@x.com")
        self.assertEqual(flow.page, "recovery_confirm")
        now["t"] += 61
        accounts.push("recovery", AccountResult(True))
        flow.resend_code()
        self.assertEqual(accounts.calls[-1], ("recovery", "b@x.com"))


class TestAutoRecheck(unittest.TestCase):
    def _to_signup(self, register_result, probe=None):
        """Script the preflight, signup and (when eligible) the single
        automatic probe before the synchronous runner consumes them."""
        flow, accounts, events = _flow()
        accounts.push("status", AccountStatus(True, email_status="new",
                                              username_available=True))
        accounts.push("register", register_result)
        if probe is not None:
            accounts.push("status", probe)
        flow.start("register")
        flow.submit_register("wilson", "w@example.com", "pw123456")
        return flow, accounts, events

    def _counts(self, accounts):
        return (len([c for c in accounts.calls if c[0] == "status"]),
                len([c for c in accounts.calls if c[0] == "register"]))

    def test_marked_duplicate_rechecks_once_and_confirmed_shows_login(self):
        flow, accounts, events = self._to_signup(
            AccountResult(False, status="email_exists",
                          detail="obfuscated_duplicate"),
            probe=AccountStatus(True, email_status="confirmed"))
        self.assertEqual(self._counts(accounts), (2, 1))
        self.assertFalse(flow._uncertain_registration)
        self.assertTrue(flow._reset_shortcut)
        errors = [e[1]["message"] for e in events if e[0] == "error"]
        self.assertTrue(any("already exists" in m for m in errors))

    def test_definitive_duplicate_does_not_recheck(self):
        flow, accounts, events = self._to_signup(
            AccountResult(False, status="email_exists",
                          detail="user_already_exists"))
        self.assertEqual(self._counts(accounts), (1, 1))
        self.assertTrue(flow._reset_shortcut)

    def test_malformed_success_rechecks_once(self):
        flow, accounts, events = self._to_signup(
            AccountResult(False, status="service_error",
                          detail="malformed_signup_user"),
            probe=AccountStatus(True, email_status="new",
                                username_available=True))
        self.assertEqual(self._counts(accounts), (2, 1))
        self.assertTrue(flow._uncertain_registration)
        self.assertTrue(any(e[0] == "uncertain" for e in events))

    def test_server_error_is_not_offline_and_rechecks(self):
        flow, accounts, events = self._to_signup(
            AccountResult(False, status="service_error",
                          detail="server:unknown_auth_error"),
            probe=AccountStatus(True, email_status="new",
                                username_available=True))
        self.assertEqual(self._counts(accounts), (2, 1))
        self.assertFalse(any(
            e[0] == "error" and "connection" in e[1]["message"].lower()
            for e in events))

    def test_connection_failure_never_auto_probes(self):
        flow, accounts, events = self._to_signup(
            AccountResult(False, status="offline", detail="connection"))
        self.assertEqual(self._counts(accounts), (1, 1))
        self.assertTrue(flow._uncertain_registration)
        errors = [e[1]["message"] for e in events if e[0] == "error"]
        self.assertTrue(any("couldn't confirm" in m.lower() for m in errors))

    def test_unconfirmed_recheck_moves_to_verify_without_sending(self):
        flow, accounts, events = self._to_signup(
            AccountResult(False, status="service_error", detail="malformed"),
            probe=AccountStatus(True, email_status="unconfirmed"))
        self.assertEqual(self._counts(accounts), (2, 1))
        pages = [e[1]["page"] for e in events if e[0] == "page"]
        self.assertEqual(pages[-1], "verify")
        self.assertFalse(any(c[0] == "resend_signup" for c in accounts.calls))
        self.assertEqual(flow._verify_email, "w@example.com")

    def test_new_unavailable_recheck_reports_username_taken(self):
        flow, accounts, events = self._to_signup(
            AccountResult(False, status="service_error", detail="malformed"),
            probe=AccountStatus(True, email_status="new",
                                username_available=False))
        self.assertEqual(self._counts(accounts), (2, 1))
        pages = [e[1]["page"] for e in events if e[0] == "page"]
        self.assertEqual(pages[-1], "register")
        self.assertFalse(flow._uncertain_registration)
        errors = [e[1]["message"] for e in events if e[0] == "error"]
        self.assertTrue(any("username is already taken" in m for m in errors))

    def test_new_available_recheck_stays_uncertain(self):
        flow, accounts, events = self._to_signup(
            AccountResult(False, status="service_error", detail="malformed"),
            probe=AccountStatus(True, email_status="new",
                                username_available=True))
        self.assertEqual(self._counts(accounts), (2, 1))
        self.assertTrue(flow._uncertain_registration)
        self.assertTrue(any(e[0] == "uncertain" for e in events))

    def test_failed_probe_retains_uncertainty(self):
        flow, accounts, events = self._to_signup(
            AccountResult(False, status="service_error", detail="malformed"),
            probe=AccountStatus(False, status="offline"))
        self.assertEqual(self._counts(accounts), (2, 1))
        self.assertTrue(flow._uncertain_registration)
        # Manual Check status is still available and does not recurse.
        accounts.push("status", AccountStatus(True, email_status="confirmed"))
        flow.check_status()
        self.assertFalse(flow._uncertain_registration)

    def test_rate_limited_probe_keeps_uncertainty_and_copy(self):
        from evolved.accounts import rate_limit_copy
        flow, accounts, events = self._to_signup(
            AccountResult(False, status="service_error", detail="malformed"),
            probe=AccountStatus(False, status="rate_limited",
                                retry_after_s=600,
                                error=rate_limit_copy(600)))
        self.assertEqual(self._counts(accounts), (2, 1))
        self.assertTrue(flow._uncertain_registration)
        errors = [e[1]["message"] for e in events if e[0] == "error"]
        self.assertTrue(any("minutes" in m for m in errors))


class TestRateLimitCooldown(unittest.TestCase):
    def test_resend_rate_limit_cooldown_uses_retry_after(self):
        from evolved.accounts import rate_limit_copy
        now = {"t": 1000.0}
        flow, accounts, events = _flow()
        flow.ctx.now = lambda: now["t"]
        accounts.push("status", AccountStatus(True, email_status="new"))
        accounts.push("register", AccountResult(
            True, status="verification_required", needs_code=True))
        flow.start("register")
        flow.submit_register("wilson", "w@example.com", "pw123456")
        now["t"] += 61
        accounts.push("resend_signup", AccountResult(
            False, status="rate_limited", retry_after_s=600,
            error=rate_limit_copy(600, email_limit=True)))
        flow.resend_code()
        self.assertGreaterEqual(flow.resend_available_in(), 599)
        errors = [e[1]["message"] for e in events if e[0] == "error"]
        self.assertTrue(any("Email limit" in m for m in errors))


class TestEntryPolish(unittest.TestCase):
    def test_back_from_register_prefills_login_identity(self):
        flow, accounts, events = _flow()
        flow.start("register")
        # Invalid email keeps the request local but still records fields.
        flow.submit_register("wilson", "w@example.com", "short")
        flow.back()
        self.assertEqual(flow.page, "login")
        pages = [e for e in events if e[0] == "page"]
        self.assertEqual(pages[-1][1].get("prefill_identity"), "w@example.com")

    def test_reset_shortcut_opens_recovery_prefilled(self):
        flow, accounts, events = _flow()
        accounts.push("status", AccountStatus(True, email_status="confirmed"))
        flow.start("register")
        flow.submit_register("wilson", "w@example.com", "pw123456")
        self.assertTrue(flow._reset_shortcut)
        flow.open_recovery_with(flow._register_fields["email"])
        self.assertEqual(flow.page, "recovery_request")
        pages = [e for e in events if e[0] == "page"]
        self.assertEqual(pages[-1][1].get("prefill_email"), "w@example.com")


class TestHomeAndDelete(unittest.TestCase):
    def _home(self):
        return {"logged_in": True, "username": "Wilson", "sync_state": "idle",
                "pending": 2, "rejected": 1}

    def test_home_without_session_shows_login(self):
        flow, accounts, events = _flow(home=lambda: {"logged_in": False})
        flow.start_home()
        self.assertEqual(flow.page, "login")

    def test_open_delete_and_gate(self):
        flow, accounts, events = _flow(home=self._home)
        flow.start_home()
        self.assertEqual(flow.page, "home")
        flow.open_delete()
        self.assertEqual(flow.page, "delete")
        self.assertEqual(flow.delete_username(), "Wilson")
        # Exact display username: wrong case is refused without a dispatch.
        flow.submit_delete("pw123456", "wilson", False)
        self.assertFalse(any(e[0] == "on_delete" for e in events))
        errors = [e[1]["message"] for e in events if e[0] == "error"]
        self.assertTrue(any("exact" in m for m in errors))
        # Empty password is refused locally.
        flow.submit_delete("", "Wilson", False)
        self.assertFalse(any(e[0] == "on_delete" for e in events))
        # Valid confirmation dispatches once with the captured choice.
        flow.submit_delete("pw123456", "Wilson", True)
        calls = [e for e in events if e[0] == "on_delete"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1],
                         {"username": "Wilson", "password": "pw123456",
                          "local": True})

    def test_delete_refusal_and_unknown_events(self):
        flow, accounts, events = _flow(home=self._home)
        flow.start_home()
        flow.open_delete()
        flow.submit_delete("pw", "Wilson", False)
        flow.apply_delete_event("refused", {"status": "invalid_credentials",
                                            "message": "Wrong password."})
        errors = [e[1]["message"] for e in events if e[0] == "error"]
        self.assertIn("Wrong password.", errors)
        # A refused delete allows a retry.
        flow.submit_delete("pw2", "Wilson", False)
        flow.apply_delete_event("unknown", {})
        errors = [e[1]["message"] for e in events if e[0] == "error"]
        self.assertTrue(any("Could not confirm deletion" in m for m in errors))
        results = [e[1] for e in events if e[0] == "delete_result"]
        self.assertEqual(results[-1]["status"], "unknown")
        self.assertTrue(results[-1]["can_check"])
        flow.check_deletion()
        self.assertTrue(any(e[0] == "on_delete_check" for e in events))

    def test_deleted_closes_window_and_exists_resumes(self):
        flow, accounts, events = _flow(home=self._home)
        flow.start_home()
        flow.open_delete()
        flow.submit_delete("pw", "Wilson", False)
        flow.apply_delete_event("exists", {})
        statuses = [e[1]["message"] for e in events if e[0] == "status"]
        self.assertTrue(any("still exists" in s for s in statuses))
        flow.apply_delete_event("deleted", {})
        closes = [e for e in events if e[0] == "close"]
        self.assertTrue(closes and closes[-1][1]["result"]["ok"])
        self.assertTrue(closes[-1][1]["result"]["deleted"])

    def test_cancel_while_pending_hides_and_does_not_cancel(self):
        flow, accounts, events = _flow(home=self._home)
        flow.start_home()
        flow.open_delete()
        flow.submit_delete("pw", "Wilson", False)
        flow.cancel()
        closes = [e for e in events if e[0] == "close"]
        self.assertTrue(closes)
        result = closes[-1][1]["result"]
        self.assertTrue(result.get("deletion_pending"))
        self.assertFalse(result.get("cancelled"))

    def test_logout_returns_to_login(self):
        flow, accounts, events = _flow(home=self._home)
        flow.start_home()
        flow.logout()
        self.assertEqual(flow.page, "login")


class TestCancel(unittest.TestCase):
    def test_cancel_emits_close_and_blocks_later_work(self):
        flow, accounts, events = _flow()
        flow.start("login")
        flow.cancel()
        closes = [e for e in events if e[0] == "close"]
        self.assertTrue(closes and closes[-1][1]["result"]["cancelled"])
        flow.submit_login("wilson", "pw123456")
        self.assertFalse(any(call[0] == "login" for call in accounts.calls))


if __name__ == "__main__":
    unittest.main()
