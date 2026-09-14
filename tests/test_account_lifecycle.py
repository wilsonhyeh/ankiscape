import json
import unittest

from evolved.accounts import (AccountResult, AccountStatus,
                              check_account_status, classify_error, register)
from evolved.net import Endpoint, NetError


def _ok(data):
    def _post(endpoint, path, payload, access_token=None, method="POST"):
        return dict(data)
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
        timeout = classify_error(NetError("http", "timed out"))
        self.assertEqual(timeout.status, "offline")
        server = classify_error(NetError("server", "503"))
        self.assertEqual(server.status, "offline")

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

    def test_ambiguous_empty_body_is_not_success(self):
        out = register(_ok({}), self.ep, username="wilson",
                       email="w@example.com", password="pw123456")
        self.assertFalse(out.ok)
        self.assertEqual(out.status, "service_error")

    def test_duplicate_email_is_typed(self):
        out = register(_raise("invalid", '{"code": "user_already_exists"}'),
                       self.ep, username="wilson", email="w@example.com",
                       password="pw123456")
        self.assertEqual(out.status, "email_exists")
        self.assertIn("already exists", out.error)

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
