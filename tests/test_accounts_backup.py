import os
import tempfile
import unittest

from evolved.accounts import (
    email_verified, login_password, login_username, logout, register,
    request_recovery, set_new_password, verify_code,
)
from evolved.auth import MemorySession
from evolved.backup import export_backup, validate_backup
from evolved.journal import Journal
from evolved.net import Endpoint, NetError


def _ok(data):
    def _post(endpoint, path, payload, access_token=None, method="POST"):
        return dict(data)
    return _post


def _fail(kind):
    def _post(endpoint, path, payload, access_token=None, method="POST"):
        raise NetError(kind, "nope", status=401 if kind in ("unauthorized",) else 0)
    return _post


class TestAccounts(unittest.TestCase):
    def setUp(self):
        self.session = MemorySession()
        self.ep = Endpoint(base_url="http://127.0.0.1:55321", project_key="pub",
                           allow_http_loopback=True)

    def test_register_needs_code_when_unconfirmed(self):
        out = register(_ok({"user": {"id": "u1"}}), self.ep, username="Wilson_Hyeh",
                       email="w@example.com", password="correct horse 9")
        self.assertTrue(out.ok and out.needs_code)

    def test_register_bad_input_generic(self):
        out = register(_ok({}), self.ep, username="ab", email="w@example.com",
                       password="x" * 300)
        self.assertFalse(out.ok)
        self.assertEqual(out.error, "invalid username or password")

    def test_login_stores_memory_session_and_gates_verification(self):
        out = login_password(_ok({"access_token": "a", "refresh_token": "r",
                                          "user": {"id": "u9"}}),
                             self.ep, email="w@example.com", password="pw123456",
                             session=self.session)
        self.assertTrue(out.ok and out.needs_code)
        self.assertTrue(self.session.logged_in)
        verified = login_password(_ok({"access_token": "a", "refresh_token": "r",
                                               "user": {"id": "u9",
                                                        "confirmed_at": "2026-01-01T00:00:00Z"}},
                                      ), self.ep, email="w@example.com", password="pw123456",
                                      session=self.session)
        self.assertTrue(verified.ok and not verified.needs_code)

    def test_login_wrong_credentials_generic(self):
        out = login_password(_fail("unauthorized"), self.ep, email="w@example.com",
                             password="pw123456", session=self.session)
        self.assertFalse(out.ok)
        self.assertEqual(out.error, "invalid username or password")
        self.assertFalse(self.session.logged_in)

    def test_username_login_uses_edge(self):
        seen = {}

        def _post(endpoint, path, payload, access_token=None, method="POST"):
            seen["path"] = path
            return {"access_token": "a", "refresh_token": "r",
                    "user": {"id": "u1", "confirmed_at": "t"}}
        out = login_username(_post, self.ep, username="Player_1", password="pw123456",
                             session=self.session)
        self.assertTrue(out.ok)
        self.assertEqual(seen["path"], "/functions/v1/username-login")

    def test_recovery_unknown_email_reads_success(self):
        out = request_recovery(_fail("not_found"), self.ep, email="nobody@example.com")
        self.assertTrue(out.ok)

    def test_verify_and_reset_password(self):
        out = verify_code(_ok({"access_token": "a", "refresh_token": "r",
                                       "user": {"id": "u1", "confirmed_at": "t"}}),
                          self.ep, email="w@example.com", code="123456",
                          kind="recovery", session=self.session)
        self.assertTrue(out.ok)
        self.assertTrue(set_new_password(_ok({}), self.ep, access_token="a",
                                         new_password="new pw 9").ok)
        self.assertTrue(logout(self.session).ok)
        self.assertFalse(self.session.logged_in)

    def test_email_verified_helper(self):
        self.assertTrue(email_verified({"confirmed_at": "x"}))
        self.assertFalse(email_verified({}))


class TestBackup(unittest.TestCase):
    def test_export_validate_roundtrip_and_restore(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        journal = Journal(os.path.join(tmp.name, "g.sqlite3"))
        self.addCleanup(journal.close)
        journal.append_operation({"op_id": "op-b1", "game_uuid": "game-bk",
                                  "device_id": "dev-a", "device_seq": 1,
                                  "lamport": 1, "kind": "review_award",
                                  "payload": {"review_key": "rk1"}})
        blob = export_backup("game-bk", journal.export_game("game-bk"))
        validate_backup(blob)
        # Tamper -> hash mismatch.
        bad = dict(blob, operations=[])
        with self.assertRaises(ValueError):
            validate_backup(bad)
        with self.assertRaises(ValueError):
            validate_backup(dict(blob, version=999))
        # Restore into a fresh journal keeps the op exactly once.
        journal2 = Journal(os.path.join(tmp.name, "g2.sqlite3"))
        self.addCleanup(journal2.close)
        counts = journal2.import_game("game-bk", validate_backup(blob))
        self.assertEqual(counts["operations"], 1)
        counts2 = journal2.import_game("game-bk", validate_backup(blob))
        self.assertEqual(counts2["operations"], 0)


if __name__ == "__main__":
    unittest.main()
