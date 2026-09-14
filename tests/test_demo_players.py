import importlib.util
import json
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _load(name, rel_path):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_ROOT, rel_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


demo_players = _load("ankiscape_demo_players", "dev/demo_players.py")
demo_traces = demo_players.demo_traces


class _FakeTransport:
    """Minimal transport double: route -> queued responses or callables."""

    def __init__(self, routes=None, statuses=None):
        self.routes = routes or {}
        self.calls = []

    def request(self, method, path, *, body=None, params=None, token="",
                service=False, headers_extra=None, expect=(200, 201, 204)):
        self.calls.append({"method": method, "path": path, "params": params,
                           "body": body, "token": token, "service": service})
        key = f"{method} {path}"
        handler = self.routes.get(key)
        if handler is None:
            raise AssertionError(f"unexpected request {key}")
        return handler(body, params) if callable(handler) else handler


class TestPlanChecksum(unittest.TestCase):
    def _plan(self, **overrides):
        traces = demo_traces.build_traces()
        target = {"kind": "local", "url": "http://127.0.0.1:55321",
                  "anon": "a", "service": "s"}
        plan = demo_players._build_plan(target, traces, None)
        plan.update(overrides)
        return plan, target, traces

    def test_checksum_covers_plan_body(self):
        plan, target, traces = self._plan()
        demo_players._verify_plan(plan, target, traces)  # no raise
        plan["actions"].append({"action": "tamper"})
        with self.assertRaises(demo_players.DemoError):
            demo_players._verify_plan(plan, target, traces)

    def test_target_mismatch_refused(self):
        plan, target, traces = self._plan()
        bad = dict(target, url="https://production.example")
        with self.assertRaises(demo_players.DemoError):
            demo_players._verify_plan(plan, bad, traces)

    def test_manifest_drift_refused(self):
        plan, target, traces = self._plan()
        plan["manifest_digest"] = "0" * 64
        body = {k: v for k, v in plan.items() if k != "checksum"}
        plan["checksum"] = demo_players._checksum(body)
        with self.assertRaises(demo_players.DemoError):
            demo_players._verify_plan(plan, target, traces)

    def test_identity_list_mismatch_refused(self):
        plan, target, traces = self._plan()
        plan["identities"] = plan["identities"][:-1]
        body = {k: v for k, v in plan.items() if k != "checksum"}
        plan["checksum"] = demo_players._checksum(body)
        with self.assertRaises(demo_players.DemoError):
            demo_players._verify_plan(plan, target, traces)

    def test_plan_identity_shape(self):
        plan, _target, traces = self._plan()
        self.assertEqual(len(plan["identities"]), 5)
        for identity in plan["identities"]:
            self.assertTrue(identity["email"].endswith(".example.invalid"))
            trace = traces[identity["display"]]
            self.assertEqual(identity["trace_hash"], trace["trace_hash"])
            self.assertEqual(identity["game_uuid"], trace["game_uuid"])


class TestSurplusOwnership(unittest.TestCase):
    def test_retire_refuses_registry_drift(self):
        registry = [{"suite_id": "hosted-v1", "suite_version": 1,
                     "username_norm": "willowmere", "user_id": "u-other"}]
        transport = _FakeTransport({
            "GET /rest/v1/fixture_registry":
                lambda body, params: registry,
        })
        item = {"suite_id": "hosted-v1", "suite_version": 1,
                "username_norm": "willowmere", "user_id": "u-expected",
                "game_uuid": "g", "reserved_email": ""}
        with self.assertRaises(demo_players.DemoError):
            demo_players._retire_surplus(transport, item)

    def test_retire_refuses_unclassified_player(self):
        registry = [{"suite_id": "hosted-v1", "suite_version": 1,
                     "username_norm": "willowmere", "user_id": "u-1"}]
        players = [{"user_id": "u-1", "username_norm": "willowmere",
                    "is_test": False}]
        transport = _FakeTransport({
            "GET /rest/v1/fixture_registry":
                lambda body, params: registry,
            "GET /rest/v1/players": lambda body, params: players,
        })
        item = {"suite_id": "hosted-v1", "suite_version": 1,
                "username_norm": "willowmere", "user_id": "u-1",
                "game_uuid": "g", "reserved_email": ""}
        with self.assertRaises(demo_players.DemoError):
            demo_players._retire_surplus(transport, item)

    def test_retire_with_exact_match_deletes_exact_rows(self):
        registry = [{"suite_id": "hosted-v1", "suite_version": 1,
                     "username_norm": "willowmere", "user_id": "u-1"}]
        players = [{"user_id": "u-1", "username_norm": "willowmere",
                    "is_test": True}]
        routes = {
            "GET /rest/v1/fixture_registry":
                lambda body, params: registry,
            "GET /rest/v1/players": lambda body, params: players,
            "DELETE /rest/v1/game_operations": lambda b, p: None,
            "DELETE /rest/v1/game_state": lambda b, p: None,
            "DELETE /rest/v1/game_checkpoints": lambda b, p: None,
            "DELETE /rest/v1/fixture_registry": lambda b, p: None,
            "DELETE /auth/v1/admin/users/u-1": lambda b, p: None,
        }
        transport = _FakeTransport(routes)
        item = {"suite_id": "hosted-v1", "suite_version": 1,
                "username_norm": "willowmere", "user_id": "u-1",
                "game_uuid": "g-1", "reserved_email": ""}
        demo_players._retire_surplus(transport, item)
        deletes = [c["path"] for c in transport.calls
                   if c["method"] == "DELETE"]
        self.assertIn("/auth/v1/admin/users/u-1", deletes)
        # Ownership scoping is exact on every delete.
        registry_delete = [c for c in transport.calls
                           if c["method"] == "DELETE"
                           and c["path"] == "/rest/v1/fixture_registry"][0]
        self.assertEqual(registry_delete["params"]["user_id"], "eq.u-1")
        self.assertEqual(registry_delete["params"]["suite_id"], "eq.hosted-v1")


class TestPlanFilePaths(unittest.TestCase):
    def test_local_and_hosted_plan_paths(self):
        self.assertTrue(demo_players._plan_path(True)
                        .endswith("demo-plan-local.json"))
        self.assertTrue(demo_players._plan_path(False)
                        .endswith("demo-plan-hosted.json"))


if __name__ == "__main__":
    unittest.main()
