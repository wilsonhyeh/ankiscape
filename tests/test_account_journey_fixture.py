# tests/test_account_journey_fixture.py - Journey fixture server contract.
"""Exercises the loopback account/Auth fixture the ui-account-lifecycle
journey uses (faults mode) against the real account/link/sync modules. This
proves the fixture server and the account lifecycle end to end without Qt,
Anki or Docker: real HTTP, real client code, deterministic local backend."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load_driver():
    spec = importlib.util.spec_from_file_location(
        "ankiscape_account_fixture_driver",
        os.path.join(ROOT, "dev", "e2e", "driver_addon", "__init__.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DRIVER = _load_driver()


class TestJourneyFixtureServer(unittest.TestCase):
    def setUp(self):
        from evolved.journal import Journal
        self.state = {"username": "accttest01"}
        self.fake = DRIVER._account_fake_reset(self.state)
        from evolved.net import Endpoint
        self.endpoint = Endpoint(
            base_url=f"http://127.0.0.1:{self.fake['port']}",
            project_key="fixture-anon", allow_http_loopback=True)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(os.path.join(self.tmp.name, "game.sqlite3"))
        self.addCleanup(self.journal.close)
        self.addCleanup(DRIVER._account_fake_stop)

    def test_full_registration_link_and_upload_path(self):
        from evolved.accounts import (check_account_status, login_password,
                                      register, verify_code)
        from evolved.auth import MemorySession
        from evolved.link import ensure_link, persist_binding, read_binding
        from evolved.net import post_json
        from evolved.service import (ServiceConfig, SyncService,
                                     make_transport)

        email = "acct_test_1@example.invalid"
        password = "correct horse 9"
        self.fake["passwords"][email] = password
        status = check_account_status(post_json, self.endpoint, email=email,
                                      username="accttest01")
        self.assertTrue(status.ok)
        self.assertEqual(status.email_status, "new")
        self.assertTrue(status.username_available)
        created = register(post_json, self.endpoint, username="accttest01",
                           email=email, password=password)
        self.assertTrue(created.ok)
        self.assertTrue(created.needs_code)
        wrong = verify_code(post_json, self.endpoint, email=email,
                            code="000000", kind="signup",
                            session=MemorySession())
        self.assertEqual(wrong.status, "expired_code")
        session = MemorySession()
        verified = verify_code(post_json, self.endpoint, email=email,
                               code="123456", kind="signup", session=session)
        self.assertTrue(verified.ok)
        self.assertTrue(session.logged_in)
        # Linkage through the authoritative RPC. The local literal survives
        # only as the offered p_game_uuid; the fake's returned uuid is the
        # identity every later call adopts (S14/S11 — remember-vs-generate).
        link = ensure_link(post_json, self.endpoint, session,
                           game_uuid="game-fixture-1")
        self.assertTrue(link.linked)
        game_uuid = link.game_uuid or "game-fixture-1"
        self.assertTrue(persist_binding(self.journal, link,
                                        game_uuid=game_uuid,
                                        user_id=session.user_id,
                                        endpoint_project=self.endpoint.base_url))
        self.assertEqual(read_binding(self.journal)["game_uuid"],
                         game_uuid)
        # Upload through the real sync service into the fixture server.
        self.journal.append_operation({
            "op_id": "op-fixture-1", "game_uuid": game_uuid,
            "device_id": "dev-a", "device_seq": 1, "lamport": 1,
            "kind": "review_award",
            "payload": {"review_key": "rk-fixture-1"}})
        svc = SyncService(
            generation=1, game_uuid=game_uuid, journal=self.journal,
            transport=make_transport(ServiceConfig(
                endpoint=self.endpoint, game_uuid=game_uuid,
                post=post_json), session),
            apply_remote=lambda page: None,
            get_user_id=lambda: session.user_id)
        out = svc.maybe_sync(manual=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["pending"], 0)
        self.assertEqual(self.journal.count_pending_operations(), 0)
        self.assertIn("op-fixture-1", self.fake["submitted"])

    def test_recovery_and_password_change(self):
        from evolved.accounts import (login_password, request_recovery,
                                      set_new_password, verify_code)
        from evolved.auth import MemorySession
        from evolved.net import post_json

        email = "acct_recovery_1@example.invalid"
        old_password = "correct horse 9"
        new_password = "correct horse 10"
        self.fake["passwords"][email] = old_password
        # The account must exist for recovery in the fixture.
        from evolved.accounts import register
        self.assertTrue(register(post_json, self.endpoint,
                                 username="acctrecovery",
                                 email=email,
                                 password=old_password).ok)
        self.assertTrue(request_recovery(post_json, self.endpoint,
                                         email=email).ok)
        recovery = MemorySession()
        verified = verify_code(post_json, self.endpoint, email=email,
                               code="246810", kind="recovery",
                               session=recovery)
        self.assertTrue(verified.ok)
        put = set_new_password(post_json, self.endpoint,
                               access_token=recovery.access_token,
                               new_password=new_password)
        self.assertTrue(put.ok)
        wrong = login_password(post_json, self.endpoint, email=email,
                               password=old_password,
                               session=MemorySession())
        self.assertEqual(wrong.status, "invalid_credentials")
        fresh = MemorySession()
        right = login_password(post_json, self.endpoint, email=email,
                               password=new_password, session=fresh)
        self.assertTrue(right.ok)


if __name__ == "__main__":
    unittest.main()
