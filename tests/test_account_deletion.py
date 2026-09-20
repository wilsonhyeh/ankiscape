import os
import tempfile
import unittest

from evolved.accounts import AccountResult
from evolved.deletion import (DeletionCoordinator, DeletionDeps,
                              canonical_game_dir, clear_marker, marker_for,
                              marker_path, read_marker, remove_game_dir,
                              resume_pending_marker, write_marker)

USER = "11111111-1111-4111-8111-111111111111"
GAME = "22222222-2222-4222-8222-222222222222"


class _FakeAccounts:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def delete_account(self, post, endpoint, *, access_token, username,
                       password):
        self.calls.append((access_token, username, password))
        return self.result


def _deps(tmp, accounts, *, delete_local=False, events=None, **overrides):
    calls = {
        "events": events if events is not None else [],
        "resumed": 0, "suspended": 0, "fenced": 0, "cleared": 0,
        "detached": 0, "pointer": 0, "stopped": 0, "awaited": 0,
        "closed": 0, "exists": None,
    }

    def _runner(work, apply):
        try:
            value = work()
        except Exception as exc:  # delivered, never raised
            value = exc
        apply(value)

    deps = DeletionDeps(
        post=lambda *a, **k: {},
        endpoint=type("EP", (), {"base_url": "https://x"})(),
        accounts=accounts,
        profile_dir=tmp,
        user_id=USER,
        username="Wilson",
        game_uuid=GAME,
        delete_local=delete_local,
        access_token="tok",
        runner=_runner,
        on_event=lambda event, payload: calls["events"].append(
            (event, dict(payload))),
        check_account_exists=lambda: calls["exists"],
        clear_identity=lambda: (calls.__setitem__("cleared",
                                                  calls["cleared"] + 1) or True),
        detach_binding=lambda: (calls.__setitem__("detached",
                                                  calls["detached"] + 1) or True),
        stop_local_work=lambda: calls.__setitem__("stopped",
                                                  calls["stopped"] + 1),
        await_workers=lambda: (calls.__setitem__("awaited",
                                                 calls["awaited"] + 1) or True),
        close_journal=lambda: calls.__setitem__("closed",
                                                calls["closed"] + 1),
        clear_pointer=lambda: (calls.__setitem__("pointer",
                                                 calls["pointer"] + 1)
                               or "done"),
        fence_epoch=lambda: calls.__setitem__("fenced", calls["fenced"] + 1),
        suspend_online=lambda: calls.__setitem__("suspended",
                                                 calls["suspended"] + 1),
        resume_online=lambda: calls.__setitem__("resumed",
                                                calls["resumed"] + 1),
    )
    for key, value in overrides.items():
        setattr(deps, key, value)
    return deps, calls


def _events(calls, name):
    return [p for e, p in calls["events"] if e == name]


class TestMarkerStore(unittest.TestCase):
    def test_roundtrip_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = marker_for(USER, GAME, endpoint_project="https://x",
                                 delete_local=True, phase="pending")
            self.assertTrue(write_marker(tmp, payload))
            marker = read_marker(tmp)
            self.assertEqual(marker["user_id"], USER)
            self.assertEqual(marker["game_uuid"], GAME)
            self.assertEqual(marker["phase"], "pending")
            self.assertTrue(marker["delete_local"])
            for secret in ("password", "email", "access_token",
                           "refresh_token"):
                self.assertNotIn(secret, marker)
            self.assertTrue(clear_marker(tmp))
            self.assertIsNone(read_marker(tmp))

    def test_pending_resumes_as_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_marker(tmp, marker_for(USER, GAME, endpoint_project="x",
                                         delete_local=False, phase="pending"))
            resume_pending_marker(tmp, read_marker(tmp))
            self.assertEqual(read_marker(tmp)["phase"], "unknown")

    def test_marker_for_another_identity_is_not_matched(self):
        from evolved.deletion import marker_matches
        marker = marker_for(USER, GAME, endpoint_project="x",
                            delete_local=False, phase="unknown")
        self.assertTrue(marker_matches(marker, user_id=USER, game_uuid=GAME))
        self.assertFalse(marker_matches(marker, user_id="other"))
        self.assertFalse(marker_matches(marker, game_uuid="other"))


class TestCanonicalPaths(unittest.TestCase):
    def test_accepts_exact_game_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = canonical_game_dir(tmp, GAME)
            self.assertEqual(
                path, os.path.join(os.path.realpath(tmp),
                                   "ankiscape-evolved", GAME))

    def test_rejects_malformed_uuid(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(canonical_game_dir(tmp, "../../escape"))
            self.assertIsNone(canonical_game_dir(tmp, "not-a-uuid"))

    def test_rejects_symlinked_game_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "ankiscape-evolved")
            os.makedirs(root)
            target = os.path.join(tmp, "elsewhere")
            os.makedirs(target)
            os.symlink(target, os.path.join(root, GAME))
            self.assertIsNone(canonical_game_dir(tmp, GAME))

    def test_remove_only_captured_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "ankiscape-evolved")
            other = "33333333-3333-4333-8333-333333333333"
            os.makedirs(os.path.join(root, GAME))
            os.makedirs(os.path.join(root, other))
            with open(os.path.join(root, GAME, "game.sqlite3"), "wb") as fh:
                fh.write(b"x")
            with open(os.path.join(root, GAME, "game.sqlite3-wal"), "wb") as fh:
                fh.write(b"x")
            self.assertTrue(remove_game_dir(canonical_game_dir(tmp, GAME)))
            self.assertFalse(os.path.exists(os.path.join(root, GAME)))
            self.assertTrue(os.path.isdir(os.path.join(root, other)))


class TestCoordinatorRefusals(unittest.TestCase):
    def test_wrong_password_is_a_known_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts = _FakeAccounts(AccountResult(
                False, status="invalid_credentials", error="wrong"))
            deps, calls = _deps(tmp, accounts)
            coord = DeletionCoordinator(deps)
            self.assertTrue(coord.begin("pw"))
            self.assertEqual(calls["cleared"], 0)
            self.assertEqual(calls["detached"], 0)
            self.assertEqual(calls["resumed"], 1)
            self.assertEqual(coord.phase, "")
            self.assertIsNone(read_marker(tmp))
            self.assertEqual(_events(calls, "refused")[-1]["status"],
                             "invalid_credentials")

    def test_demo_and_rate_limit_do_not_touch_local_state(self):
        for status in ("demo_immutable", "rate_limited",
                       "invalid_confirmation", "account_unavailable",
                       "service_error"):
            with self.subTest(status=status), \
                    tempfile.TemporaryDirectory() as tmp:
                accounts = _FakeAccounts(AccountResult(
                    False, status=status, retry_after_s=600))
                deps, calls = _deps(tmp, accounts, delete_local=True)
                coord = DeletionCoordinator(deps)
                coord.begin("pw")
                self.assertEqual(calls["cleared"], 0)
                self.assertEqual(calls["stopped"], 0)
                self.assertEqual(calls["pointer"], 0)
                self.assertIsNone(read_marker(tmp))
                self.assertEqual(coord.phase, "")

    def test_username_mismatch_refuses_before_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts = _FakeAccounts(AccountResult(True, status="deleted"))
            deps, calls = _deps(tmp, accounts)
            coord = DeletionCoordinator(deps)
            self.assertFalse(coord.begin("pw", username="Someone Else"))
            self.assertEqual(accounts.calls, [])
            self.assertEqual(calls["fenced"], 0)
            self.assertIsNone(read_marker(tmp))

    def test_double_dispatch_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts = _FakeAccounts(AccountResult(
                False, status="delete_unknown"))
            deps, calls = _deps(tmp, accounts)
            coord = DeletionCoordinator(deps)
            self.assertTrue(coord.begin("pw"))
            self.assertFalse(coord.begin("pw"))
            self.assertEqual(len(accounts.calls), 1)

    def test_empty_password_refuses_locally(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts = _FakeAccounts(AccountResult(True, status="deleted"))
            deps, calls = _deps(tmp, accounts)
            coord = DeletionCoordinator(deps)
            self.assertFalse(coord.begin(""))
            self.assertEqual(accounts.calls, [])


class TestCoordinatorKeepLocal(unittest.TestCase):
    def test_success_keep_local_cleans_identity_and_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "ankiscape-evolved", GAME))
            accounts = _FakeAccounts(AccountResult(True, status="deleted"))
            deps, calls = _deps(tmp, accounts)
            coord = DeletionCoordinator(deps)
            self.assertTrue(coord.begin("pw"))
            self.assertEqual(calls["fenced"], 1)
            self.assertEqual(calls["suspended"], 1)
            self.assertEqual(calls["cleared"], 1)
            self.assertEqual(calls["detached"], 1)
            self.assertEqual(calls["stopped"], 0)  # keep-local leaves files
            self.assertIsNone(read_marker(tmp))
            self.assertEqual(coord.phase, "done")
            # Local game directory retained for local-only play.
            self.assertTrue(os.path.isdir(
                os.path.join(tmp, "ankiscape-evolved", GAME)))
            self.assertEqual(_events(calls, "deleted")[-1], {})

    def test_server_deleted_with_local_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = os.path.join(tmp, "ankiscape-evolved", GAME)
            os.makedirs(game_dir)
            accounts = _FakeAccounts(AccountResult(True, status="deleted"))
            deps, calls = _deps(tmp, accounts, delete_local=True)
            coord = DeletionCoordinator(deps)
            self.assertTrue(coord.begin("pw"))
            self.assertEqual(calls["stopped"], 1)
            self.assertEqual(calls["awaited"], 1)
            self.assertEqual(calls["closed"], 1)
            self.assertEqual(calls["pointer"], 1)
            self.assertFalse(os.path.exists(game_dir))
            self.assertIsNone(read_marker(tmp))
            self.assertEqual(coord.phase, "done")


class TestCoordinatorUnknown(unittest.TestCase):
    def test_unknown_retains_marker_and_suspends(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts = _FakeAccounts(AccountResult(
                False, status="delete_unknown"))
            deps, calls = _deps(tmp, accounts)
            coord = DeletionCoordinator(deps)
            coord.begin("pw")
            self.assertEqual(coord.phase, "unknown")
            self.assertEqual(read_marker(tmp)["phase"], "unknown")
            self.assertEqual(calls["cleared"], 0)
            self.assertEqual(calls["resumed"], 0)
            self.assertTrue(_events(calls, "unknown"))

    def test_offline_response_is_unknown_not_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts = _FakeAccounts(AccountResult(False, status="offline"))
            deps, calls = _deps(tmp, accounts)
            coord = DeletionCoordinator(deps)
            coord.begin("pw")
            self.assertEqual(coord.phase, "unknown")
            self.assertIsNotNone(read_marker(tmp))

    def test_reconcile_inconclusive_keeps_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts = _FakeAccounts(AccountResult(
                False, status="delete_unknown"))
            deps, calls = _deps(tmp, accounts)
            deps.check_account_exists = lambda: None
            coord = DeletionCoordinator(deps)
            coord.begin("pw")
            self.assertIsNone(coord.reconcile())
            self.assertEqual(coord.phase, "unknown")
            self.assertIsNotNone(read_marker(tmp))

    def test_reconcile_absent_runs_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = os.path.join(tmp, "ankiscape-evolved", GAME)
            os.makedirs(game_dir)
            accounts = _FakeAccounts(AccountResult(
                False, status="delete_unknown"))
            deps, calls = _deps(tmp, accounts, delete_local=True)
            deps.check_account_exists = lambda: False
            coord = DeletionCoordinator(deps)
            coord.begin("pw")
            self.assertFalse(coord.reconcile())
            self.assertEqual(coord.phase, "done")
            self.assertFalse(os.path.exists(game_dir))
            self.assertIsNone(read_marker(tmp))

    def test_reconcile_exists_clears_marker_and_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts = _FakeAccounts(AccountResult(
                False, status="delete_unknown"))
            deps, calls = _deps(tmp, accounts)
            deps.check_account_exists = lambda: True
            coord = DeletionCoordinator(deps)
            coord.begin("pw")
            self.assertTrue(coord.reconcile())
            self.assertEqual(coord.phase, "")
            self.assertIsNone(read_marker(tmp))
            self.assertEqual(calls["resumed"], 1)
            # A fresh confirmation is now allowed.
            self.assertTrue(coord.begin("pw"))


class TestCoordinatorCleanupFailures(unittest.TestCase):
    def test_worker_in_flight_retains_local_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = os.path.join(tmp, "ankiscape-evolved", GAME)
            os.makedirs(game_dir)
            accounts = _FakeAccounts(AccountResult(True, status="deleted"))
            deps, calls = _deps(tmp, accounts, delete_local=True)
            deps.await_workers = lambda: False
            coord = DeletionCoordinator(deps)
            coord.begin("pw")
            self.assertEqual(coord.phase, "cleanup_pending")
            self.assertIsNotNone(read_marker(tmp))
            self.assertTrue(os.path.isdir(game_dir))
            self.assertIn("local",
                          _events(calls, "cleanup_incomplete")[-1]
                          ["problems"])

    def test_credential_failure_is_cleanup_pending_and_local_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = os.path.join(tmp, "ankiscape-evolved", GAME)
            os.makedirs(game_dir)
            accounts = _FakeAccounts(AccountResult(True, status="deleted"))
            deps, calls = _deps(tmp, accounts, delete_local=True)
            deps.clear_identity = lambda: False
            coord = DeletionCoordinator(deps)
            coord.begin("pw")
            self.assertEqual(coord.phase, "cleanup_pending")
            self.assertIn("credentials",
                          _events(calls, "cleanup_incomplete")[-1]
                          ["problems"])
            self.assertIsNotNone(read_marker(tmp))
            # Local-only retry must not re-issue the server delete.
            deps.clear_identity = lambda: True
            self.assertTrue(coord.retry_local_cleanup())
            self.assertEqual(len(accounts.calls), 1)
            self.assertEqual(coord.phase, "done")
            self.assertIsNone(read_marker(tmp))

    def test_deferred_pointer_keeps_marker_until_profile_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "ankiscape-evolved", GAME))
            accounts = _FakeAccounts(AccountResult(True, status="deleted"))
            deps, calls = _deps(tmp, accounts, delete_local=True)
            deps.clear_pointer = lambda: "deferred"
            coord = DeletionCoordinator(deps)
            coord.begin("pw")
            self.assertEqual(coord.phase, "cleanup_pending")
            self.assertEqual(read_marker(tmp)["phase"], "cleanup_pending")
            deps.clear_pointer = lambda: "done"
            self.assertTrue(coord.retry_local_cleanup())
            self.assertIsNone(read_marker(tmp))

    def test_local_retry_re_clears_credentials_before_reporting_deleted(self):
        # delete_local=False used to short-circuit the retry to success: it
        # cleared the marker and emitted "deleted" before ever re-attempting
        # clear_identity(), so a user whose credential clear had failed could
        # retry, be told the deletion succeeded, and leave credentials in the
        # OS vault. The retry must re-run the identity steps and only report
        # "deleted" once they actually succeed. The sibling test above uses
        # delete_local=True and so never exercised this branch.
        with tempfile.TemporaryDirectory() as tmp:
            accounts = _FakeAccounts(AccountResult(True, status="deleted"))
            deps, calls = _deps(tmp, accounts, delete_local=False)
            deps.clear_identity = lambda: False
            coord = DeletionCoordinator(deps)
            coord.begin("pw")
            self.assertEqual(coord.phase, "cleanup_pending")
            self.assertIn("credentials",
                          _events(calls, "cleanup_incomplete")[-1]
                          ["problems"])
            # Still failing: the retry must NOT claim the account is deleted.
            self.assertFalse(coord.retry_local_cleanup())
            self.assertEqual(coord.phase, "cleanup_pending")
            self.assertIsNotNone(read_marker(tmp))
            # Once the credential clear succeeds, the retry completes.
            deps.clear_identity = lambda: True
            self.assertTrue(coord.retry_local_cleanup())
            self.assertEqual(coord.phase, "done")
            self.assertIsNone(read_marker(tmp))
            # Local-only retry must never re-issue the server delete.
            self.assertEqual(len(accounts.calls), 1)


if __name__ == "__main__":
    unittest.main()
