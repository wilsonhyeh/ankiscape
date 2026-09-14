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


def _flow(*, runner=None, install_raises=False):
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
        accounts.push("recovery", AccountResult(True))
        flow.resend_code()
        self.assertEqual(accounts.calls[-1][0], "recovery")


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
