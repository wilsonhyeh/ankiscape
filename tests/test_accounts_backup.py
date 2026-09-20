import os
import tempfile
import unittest

from evolved.accounts import (
    email_verified, login_password, login_username, logout, register,
    request_recovery, set_new_password, verify_code,
)
from evolved.auth import MemorySession
from evolved.backup import export_backup, validate_backup
from evolved.data import load_rules
from evolved.engine import EngineConfig, EvolvedEngine
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

    def test_register_bad_input_typed(self):
        out = register(_ok({}), self.ep, username="ab", email="w@example.com",
                       password="x" * 300)
        self.assertFalse(out.ok)
        self.assertEqual(out.status, "invalid_username")
        self.assertTrue(out.error)
        weak = register(_ok({}), self.ep, username="wilson_hyeh",
                        email="w@example.com", password="short")
        self.assertFalse(weak.ok)
        self.assertEqual(weak.status, "weak_password")

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

    def test_login_wrong_credentials_typed(self):
        out = login_password(_fail("unauthorized"), self.ep, email="w@example.com",
                             password="pw123456", session=self.session)
        self.assertFalse(out.ok)
        self.assertEqual(out.status, "invalid_credentials")
        self.assertTrue(out.error)
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

    def test_rollback_rehearsal_restored_projection_equals_reference(self):
        """ROLLBACK.md "Reproduce backup restore" acceptance criterion.

        The rehearsal's stated verification is that "the restored state must
        equal the reference reducer over the same operations
        (``engine.projection()`` boundary: xp_micro, inventory, levels,
        revision)". The sibling test above asserts only operation COUNTS, so
        that criterion was previously unpinned -- a restore could have restored
        the right number of operations while producing a different world.

        Performed on disposable journals in a temp dir, per ROLLBACK.md step 3
        ("validate on a disposable profile only").
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rules = load_rules()
        game = "game-rehearsal"

        source = Journal(os.path.join(tmp.name, "source.sqlite3"))
        self.addCleanup(source.close)
        engine = EvolvedEngine(
            EngineConfig(game_uuid=game, device_id="dev-a",
                         activated_at=1000, rules=rules), source)
        for i in range(1, 6):
            source.append_operation({
                "op_id": "op-%d" % i, "game_uuid": game, "device_id": "dev-a",
                "device_seq": i, "lamport": i, "kind": "review_award",
                "payload": {"review_key": "rk%d" % i,
                            "review_ts": 1850000000 + i, "rating": 3,
                            "review_kind": "review", "provenance": "direct",
                            "reward_policy": 2, "skill": "mining",
                            "resource": "Rune essence"}})
        reference = engine.projection()
        self.assertGreater(reference["revision"], 0)
        self.assertGreater(reference["xp_micro"]["mining"], 0)

        blob = export_backup(game, source.export_game(game))

        restored = Journal(os.path.join(tmp.name, "restored.sqlite3"))
        self.addCleanup(restored.close)
        restored.import_game(game, validate_backup(blob))
        rebuilt = EvolvedEngine(
            EngineConfig(game_uuid=game, device_id="dev-a",
                         activated_at=1000, rules=rules), restored)

        after = rebuilt.projection()
        for dimension in ("xp_micro", "inventory", "levels", "revision"):
            self.assertEqual(after[dimension], reference[dimension], dimension)
        self.assertEqual(after, reference)


if __name__ == "__main__":
    unittest.main()
