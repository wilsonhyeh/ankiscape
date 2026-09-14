import json
import unittest

from evolved.accounts import (AccountResult, AccountStatus,
                              check_account_status, classify_error, register,
                              resend_signup_code, rate_limit_copy)
from evolved.auth import MemorySession
from evolved.net import Endpoint, NetError


def _ok(data):
    def _post(endpoint, path, payload, access_token=None, method="POST"):
        return dict(data) if isinstance(data, dict) else data
    return _post


def _raise(kind, detail="", status=0, retry_after=None, code=""):
    def _post(endpoint, path, payload, access_token=None, method="POST"):
        raise NetError(kind, detail, status=status, retry_after=retry_after,
                       code=code)
    return _post


class TestNetErrorCodes(unittest.TestCase):
    def test_http_error_body_code_is_extracted_and_bounded(self):
        import io
        import urllib.error
        from evolved.net import MAX_RESPONSE_BYTES, _map_http_error
        body = json.dumps({"code": "user_already_exists",
                           "message": "x" * 900}).encode()
        exc = urllib.error.HTTPError("https://x", 422, "unprocessable",
                                     {}, io.BytesIO(body))
        mapped = _map_http_error(exc)
        self.assertEqual(mapped.kind, "invalid")
        self.assertEqual(mapped.code, "user_already_exists")
        self.assertLessEqual(len(mapped.detail), 500)
        _ = MAX_RESPONSE_BYTES


class TestClassifyError(unittest.TestCase):
    def test_allowlisted_auth_codes(self):
        cases = {
            "user_already_exists": "email_exists",
            "email_exists": "email_exists",
            "username_taken": "username_taken",
            "weak_password": "weak_password",
            "email_address_invalid": "invalid_email",
            "invalid_grant": "invalid_credentials",
            "email_not_confirmed": "verification_required",
            "otp_expired": "expired_code",
            "invalid_token": "invalid_code",
            "over_email_send_rate_limit": "rate_limited",
        }
        for code, want in cases.items():
            exc = NetError("invalid", json.dumps({"code": code}))
            out = classify_error(exc)
            self.assertEqual(out.status, want, msg=code)
            self.assertFalse(out.ok)
            # Raw server text never reaches user copy.
            self.assertNotIn(code, out.error)

    def test_unknown_code_is_service_error_without_copy_leak(self):
        exc = NetError("invalid", json.dumps(
            {"code": "totally_new_code", "message": "secret detail"}))
        out = classify_error(exc)
        self.assertEqual(out.status, "service_error")
        self.assertNotIn("secret detail", out.error)

    def test_transport_failures_are_not_wrong_password(self):
        offline = classify_error(NetError("connection", "reset"))
        self.assertEqual(offline.status, "offline")
        timeout = classify_error(NetError("timeout", "timed out"))
        self.assertEqual(timeout.status, "offline")
        # An HTTP answer (including 5xx) is a service answer, not a network
        # failure: network wording must not cover server errors.
        server = classify_error(NetError("server", "503"))
        self.assertEqual(server.status, "service_error")
        http = classify_error(NetError("http", "418"))
        self.assertEqual(http.status, "service_error")

    def test_rate_limit_carries_retry_after(self):
        out = classify_error(NetError("rate_limited", "", retry_after=42))
        self.assertEqual(out.status, "rate_limited")
        self.assertEqual(out.retry_after_s, 42)


class TestRegisterOutcomes(unittest.TestCase):
    def setUp(self):
        self.ep = Endpoint(base_url="https://x", project_key="pub")

    def test_unconfirmed_signup_requires_code(self):
        out = register(_ok({"user": {"id": "u1"}}), self.ep,
                       username="wilson", email="w@example.com",
                       password="pw123456")
        self.assertTrue(out.ok)
        self.assertTrue(out.needs_code)
        self.assertEqual(out.status, "verification_required")

    def test_auto_confirmed_signup_is_success(self):
        out = register(_ok({"user": {"id": "u1",
                                     "confirmed_at": "2026-01-01T00:00:00Z"}}),
                       self.ep, username="wilson", email="w@example.com",
                       password="pw123456")
        self.assertTrue(out.ok)
        self.assertFalse(out.needs_code)
        self.assertEqual(out.status, "success")

    def test_flat_unconfirmed_signup_requires_code(self):
        # Hosted GoTrue returns the user object at the top level.
        out = register(_ok({"id": "u1", "aud": "authenticated",
                            "confirmation_sent_at": "2026-09-13T00:00:00Z",
                            "identities": [{"id": "i1"}]}), self.ep,
                       username="wilson", email="w@example.com",
                       password="pw123456")
        self.assertTrue(out.ok)
        self.assertTrue(out.needs_code)
        self.assertEqual(out.status, "verification_required")
        self.assertEqual(out.session_user_id, "u1")

    def test_flat_confirmed_signup_is_success(self):
        out = register(_ok({"id": "u1",
                            "confirmed_at": "2026-09-13T00:00:00Z",
                            "identities": [{"id": "i1"}]}), self.ep,
                       username="wilson", email="w@example.com",
                       password="pw123456")
        self.assertTrue(out.ok)
        self.assertFalse(out.needs_code)
        self.assertEqual(out.status, "success")

    def test_obfuscated_duplicate_is_marked(self):
        out = register(_ok({"id": "u1", "identities": []}), self.ep,
                       username="wilson", email="w@example.com",
                       password="pw123456")
        self.assertFalse(out.ok)
        self.assertEqual(out.status, "email_exists")
        self.assertEqual(out.detail, "obfuscated_duplicate")

    def test_duplicate_email_is_typed(self):
        out = register(_raise("invalid", '{"code": "user_already_exists"}'),
                       self.ep, username="wilson", email="w@example.com",
                       password="pw123456")
        self.assertEqual(out.status, "email_exists")
        self.assertIn("already exists", out.error)
        self.assertEqual(out.detail, "user_already_exists")

    def test_generic_conflict_is_marked_signup_conflict(self):
        out = register(_raise("conflict", "409"), self.ep, username="wilson",
                       email="w@example.com", password="pw123456")
        self.assertEqual(out.status, "email_exists")
        self.assertEqual(out.detail, "signup_conflict")

    def test_malformed_shapes_stay_bounded_service_errors(self):
        ep = self.ep
        cases = [
            [],
            {"user": "not-an-object"},
            {"user": {"id": 42}},
            {"user": {"id": ""}},
            {"user": {}},
            {"id": ""},
            "scalar",
            None,
        ]
        for data in cases:
            out = register(_ok(data), ep, username="wilson",
                           email="w@example.com", password="pw123456")
            self.assertFalse(out.ok, msg=repr(data))
            self.assertEqual(out.status, "service_error", msg=repr(data))
            self.assertLessEqual(len(out.detail), 200)

    def test_unknown_code_sentinels_never_reach_diagnostics(self):
        sentinel = "w@example.com:hunter2"
        exc = NetError("invalid", json.dumps(
            {"code": sentinel, "message": f"password={sentinel}"}))
        out = classify_error(exc, context="register")
        self.assertEqual(out.status, "service_error")
        self.assertNotIn(sentinel, out.detail)
        self.assertNotIn(sentinel, out.error)
        self.assertEqual(out.detail, "unknown_auth_error")

    def test_unexpected_exception_label_is_closed(self):
        from evolved.accounts import _UNEXPECTED_EXCEPTION
        self.assertEqual(_UNEXPECTED_EXCEPTION, "unexpected_exception")

    def test_rate_limited_unknown_code_is_closed(self):
        exc = NetError("rate_limited", json.dumps(
            {"code": "made_up_limit", "message": "secret"}))
        out = classify_error(exc)
        self.assertEqual(out.status, "rate_limited")
        self.assertNotIn("made_up_limit", out.detail)
        self.assertNotIn("secret", out.error)

    def test_ambiguous_empty_body_is_not_success(self):
        out = register(_ok({}), self.ep, username="wilson",
                       email="w@example.com", password="pw123456")
        self.assertFalse(out.ok)
        self.assertEqual(out.status, "service_error")

    def test_username_race_is_typed(self):
        out = register(_raise("conflict", '{"code": "username_taken"}'),
                       self.ep, username="wilson", email="w@example.com",
                       password="pw123456")
        self.assertEqual(out.status, "username_taken")

    def test_local_validation_is_typed_and_requestless(self):
        seen = []

        def _post(endpoint, path, payload, access_token=None, method="POST"):
            seen.append(path)
            return {}

        out = register(_post, self.ep, username="ab", email="w@example.com",
                       password="pw123456")
        self.assertEqual(out.status, "invalid_username")
        out = register(_post, self.ep, username="wilson", email="bad",
                       password="pw123456")
        self.assertEqual(out.status, "invalid_email")
        out = register(_post, self.ep, username="wilson", email="w@example.com",
                       password="short")
        self.assertEqual(out.status, "weak_password")
        self.assertEqual(seen, [])


class TestCheckAccountStatus(unittest.TestCase):
    def setUp(self):
        self.ep = Endpoint(base_url="https://x", project_key="pub")

    def _status(self, **fields):
        base = {"email_status": "new", "username_available": True}
        base.update(fields)
        return _ok(base)

    def test_new_and_available(self):
        out = check_account_status(self._status(), self.ep,
                                   email="w@example.com", username="wilson")
        self.assertTrue(out.ok)
        self.assertEqual(out.email_status, "new")
        self.assertTrue(out.username_available)

    def test_unconfirmed_and_taken(self):
        out = check_account_status(
            self._status(email_status="unconfirmed", username_available=False),
            self.ep, email="w@example.com", username="wilson")
        self.assertEqual(out.email_status, "unconfirmed")
        self.assertFalse(out.username_available)

    def test_missing_function_is_service_error_not_unknown(self):
        out = check_account_status(_raise("not_found", ""), self.ep,
                                   email="w@example.com")
        self.assertFalse(out.ok)
        self.assertEqual(out.status, "service_error")
        self.assertEqual(out.detail, "account_status_unavailable")

    def test_rate_limited_carries_retry_after(self):
        out = check_account_status(
            _raise("rate_limited", retry_after=30), self.ep,
            email="w@example.com")
        self.assertEqual(out.status, "rate_limited")
        self.assertEqual(out.retry_after_s, 30)

    def test_offline_maps_to_offline(self):
        out = check_account_status(_raise("connection", "reset"), self.ep,
                                   email="w@example.com")
        self.assertEqual(out.status, "offline")

    def test_malformed_status_is_service_error(self):
        out = check_account_status(_ok({"email_status": "maybe"}), self.ep,
                                   email="w@example.com")
        self.assertFalse(out.ok)
        self.assertEqual(out.status, "service_error")

    def test_invalid_input_is_local(self):
        seen = []

        def _post(endpoint, path, payload, access_token=None, method="POST"):
            seen.append(path)
            return {}

        out = check_account_status(_post, self.ep, email="nope")
        self.assertEqual(out.status, "invalid_email")
        self.assertEqual(seen, [])


class TestResendSignupCode(unittest.TestCase):
    def setUp(self):
        self.ep = Endpoint(base_url="https://x", project_key="pub")

    def test_posts_signup_type_to_resend_endpoint(self):
        seen = []

        def _post(endpoint, path, payload, access_token=None, method="POST"):
            seen.append((path, dict(payload)))
            return {}

        out = resend_signup_code(_post, self.ep, email="w@example.com")
        self.assertTrue(out.ok)
        self.assertEqual(seen[0][0], "/auth/v1/resend")
        self.assertEqual(seen[0][1].get("type"), "signup")
        self.assertEqual(seen[0][1].get("email"), "w@example.com")

    def test_rate_limit_is_typed_with_retry_after(self):
        out = resend_signup_code(
            _raise("rate_limited", retry_after=600,
                   code="over_email_send_rate_limit"),
            self.ep, email="w@example.com")
        self.assertEqual(out.status, "rate_limited")
        self.assertEqual(out.retry_after_s, 600)
        self.assertIn("Email limit reached", out.error)
        self.assertIn("10 minutes", out.error)

    def test_unknown_email_reads_accepted(self):
        out = resend_signup_code(_raise("not_found", "", status=404),
                                 self.ep, email="w@example.com")
        self.assertTrue(out.ok)

    def test_invalid_email_is_local_and_requestless(self):
        seen = []

        def _post(endpoint, path, payload, access_token=None, method="POST"):
            seen.append(path)
            return {}

        out = resend_signup_code(_post, self.ep, email="bad")
        self.assertEqual(out.status, "invalid_email")
        self.assertEqual(seen, [])


class TestRateLimitCopy(unittest.TestCase):
    def test_email_limit_uses_minute_copy(self):
        copy = rate_limit_copy(600, email_limit=True)
        self.assertIn("Email limit reached", copy)
        self.assertIn("about 10 minutes", copy)

    def test_minutes_round_up(self):
        self.assertIn("about 3 minutes",
                      rate_limit_copy(121, email_limit=True))

    def test_generic_for_other_429s(self):
        copy = rate_limit_copy(600)
        self.assertIn("Too many requests", copy)
        self.assertNotIn("Email", copy)

    def test_short_copy_below_two_minutes(self):
        self.assertEqual(rate_limit_copy(30),
                         "Too many attempts. Wait a moment and try again.")

    def test_unknown_never_invents_a_reset_time(self):
        for value in (0, None, -5, "not-a-number"):
            copy = rate_limit_copy(value)
            self.assertNotIn("minute", copy)
            self.assertIn("later", copy)

    def test_classify_identifies_email_send_limit(self):
        exc = NetError("rate_limited", "", retry_after=300,
                       code="over_email_send_rate_limit")
        out = classify_error(exc)
        self.assertEqual(out.status, "rate_limited")
        self.assertIn("Email limit", out.error)


class TestDeleteAccount(unittest.TestCase):
    def setUp(self):
        self.ep = Endpoint(base_url="https://x", project_key="pub")

    def _delete(self, post, **kwargs):
        from evolved.accounts import delete_account
        args = {"access_token": "tok", "username": "Wilson",
                "password": "pw123456"}
        args.update(kwargs)
        return delete_account(post, self.ep, **args)

    def test_success_requires_literal_deleted_true(self):
        out = self._delete(_ok({"deleted": True}))
        self.assertTrue(out.ok)
        self.assertEqual(out.status, "deleted")
        out = self._delete(_ok({"ok": True}))
        self.assertFalse(out.ok)
        self.assertEqual(out.status, "delete_unknown")

    def test_closed_function_codes_map_exactly(self):
        cases = {
            "invalid_credentials": "invalid_credentials",
            "invalid_confirmation": "invalid_confirmation",
            "account_unavailable": "account_unavailable",
            "demo_immutable": "demo_immutable",
            "invalid_session": "invalid_session",
        }
        for code, want in cases.items():
            body = json.dumps({"error": code})
            out = self._delete(_raise("invalid", body, status=400, code=code))
            self.assertEqual(out.status, want, msg=code)

    def test_plain_401_is_not_wrong_password(self):
        out = self._delete(_raise("unauthorized", "", status=401))
        self.assertEqual(out.status, "invalid_session")

    def test_delete_unknown_code_and_timeout(self):
        out = self._delete(_raise("server", '{"error":"delete_unknown"}',
                                  status=504, code="delete_unknown"))
        self.assertEqual(out.status, "delete_unknown")
        out = self._delete(_raise("server", "", status=504))
        self.assertEqual(out.status, "service_error")

    def test_rate_limit_carries_retry_after(self):
        out = self._delete(_raise("rate_limited", "", retry_after=900,
                                  code="rate_limited"))
        self.assertEqual(out.status, "rate_limited")
        self.assertEqual(out.retry_after_s, 900)

    def test_connection_failure_is_offline(self):
        out = self._delete(_raise("connection", "reset"))
        self.assertEqual(out.status, "offline")

    def test_local_validation_is_requestless(self):
        seen = []

        def _post(endpoint, path, payload, access_token=None, method="POST"):
            seen.append(path)
            return {"deleted": True}

        self.assertEqual(self._delete(_post, access_token="").status,
                         "invalid_session")
        self.assertEqual(self._delete(_post, username=" ").status,
                         "invalid_confirmation")
        self.assertEqual(self._delete(_post, password="").status,
                         "invalid_credentials")
        self.assertEqual(seen, [])


class TestRefreshAccessToken(unittest.TestCase):
    def setUp(self):
        self.ep = Endpoint(base_url="https://x", project_key="pub")

    def _session(self):
        s = MemorySession()
        s.set(access_token="old", refresh_token="r1", user_id="u1",
              username="wilson")
        return s

    def test_same_identity_rotates_tokens(self):
        from evolved.accounts import refresh_access_token
        s = self._session()
        data = {"access_token": "new", "refresh_token": "r2",
                "user": {"id": "u1"}}
        self.assertTrue(refresh_access_token(_ok(data), self.ep, s))
        self.assertEqual((s.access_token, s.refresh_token, s.user_id),
                         ("new", "r2", "u1"))
        self.assertEqual(s.username, "wilson")

    def test_mismatched_identity_clears_and_never_installs(self):
        from evolved.accounts import refresh_access_token
        s = self._session()
        data = {"access_token": "new", "refresh_token": "r2",
                "user": {"id": "other"}}
        self.assertFalse(refresh_access_token(_ok(data), self.ep, s))
        self.assertFalse(s.logged_in)
        self.assertIsNone(s.access_token)

    def test_malformed_response_installs_nothing_and_keeps_session(self):
        from evolved.accounts import refresh_access_token
        s = self._session()
        data = {"user": {"id": "u1"}}  # no tokens
        self.assertFalse(refresh_access_token(_ok(data), self.ep, s))
        self.assertTrue(s.logged_in)
        self.assertEqual(s.access_token, "old")

    def test_logout_during_refresh_is_not_undone(self):
        from evolved.accounts import refresh_access_token
        s = self._session()

        def _post(endpoint, path, payload, access_token=None, method="POST"):
            s.clear()  # explicit logout lands while the request is in flight
            return {"access_token": "new", "refresh_token": "r2",
                    "user": {"id": "u1"}}

        self.assertFalse(refresh_access_token(_post, self.ep, s))
        self.assertFalse(s.logged_in)

    def test_login_b_during_refresh_a_is_not_overwritten(self):
        from evolved.accounts import refresh_access_token
        s = self._session()

        def _post(endpoint, path, payload, access_token=None, method="POST"):
            s.set(access_token="b", refresh_token="rb", user_id="b",
                  username="b")
            return {"access_token": "a2", "refresh_token": "ra2",
                    "user": {"id": "u1"}}

        self.assertFalse(refresh_access_token(_post, self.ep, s))
        self.assertEqual((s.user_id, s.access_token, s.refresh_token),
                         ("b", "b", "rb"))

    def test_definitive_invalid_refresh_clears_original(self):
        from evolved.accounts import refresh_access_token
        s = self._session()
        self.assertFalse(refresh_access_token(_raise("unauthorized"), self.ep, s))
        self.assertFalse(s.logged_in)

    def test_connection_failure_keeps_session(self):
        from evolved.accounts import refresh_access_token
        s = self._session()
        self.assertFalse(refresh_access_token(
            _raise("connection", "reset"), self.ep, s))
        self.assertTrue(s.logged_in)


class TestOutcomeModelShape(unittest.TestCase):
    def test_result_carries_typed_status_default(self):
        out = AccountResult(True)
        self.assertTrue(out.ok)
        self.assertFalse(out.needs_code)
        self.assertEqual(out.status, "success")

    def test_status_model_fields(self):
        status = AccountStatus(True, status="success", email_status="new")
        self.assertEqual(status.email_status, "new")


if __name__ == "__main__":
    unittest.main()
