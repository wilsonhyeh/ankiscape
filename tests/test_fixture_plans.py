# tests/test_fixture_plans.py - Fixture suites: historical + active demos.
"""The hosted-v1 24-account suite is RETIRED (kept as historical data only;
its seeding tools refuse to run). The active permanent population is the
public-demo-v1 five labeled demo players. These checks pin both facts:
historical data is preserved without credentials, and the demo suite is
deterministic, bounded and fully covered."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name,
                                                  os.path.join(ROOT, relative))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HISTORICAL = _load("ankiscape_fixture_traces_test", "dev/fixture_traces.py")
DEMOS = _load("ankiscape_demo_traces_test", "dev/demo_traces.py")
SEED = _load("ankiscape_seed_guard_test", "dev/seed_hosted_fixtures.py")
HOSTED_E2E = _load("ankiscape_hosted_e2e_guard_test",
                   "dev/hosted_fixture_e2e.py")

FORBIDDEN_KEYS = {"password", "secret", "service_role", "access_token",
                  "refresh_token", "anon_key", "apikey"}


class HistoricalHostedSuiteTests(unittest.TestCase):
    """The old manifest remains readable historical data, not executable."""

    @classmethod
    def setUpClass(cls):
        cls.suite = HISTORICAL.load_suite()

    def test_manifest_preserved_with_twenty_four_names(self):
        names = self.suite["display_names"]
        self.assertEqual(len(names), 24)
        self.assertEqual(len(set(HISTORICAL.username_norm(n)
                                 for n in names)), 24)

    def test_manifest_carries_no_credentials(self):
        def _walk(value, path="suite"):
            if isinstance(value, dict):
                for key, child in value.items():
                    self.assertNotIn(str(key).lower(), FORBIDDEN_KEYS,
                                     f"credential field at {path}.{key}")
                    _walk(child, f"{path}.{key}")
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    _walk(child, f"{path}[{index}]")
        _walk(self.suite)

    def test_seeding_tools_refuse_to_recreate_retired_users(self):
        self.assertEqual(SEED.main([]), 2)
        self.assertEqual(HOSTED_E2E.main([]), 2)


class ActiveDemoSuiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite = DEMOS.load_suite()
        cls.traces = DEMOS.build_traces(tuple(cls.suite["display_names"]))

    def test_exactly_five_unique_demo_names(self):
        names = self.suite["display_names"]
        self.assertEqual(len(names), 5)
        self.assertEqual(sorted(names), sorted(DEMOS.DISPLAY_NAMES))
        norms = [DEMOS.username_norm(n) for n in names]
        self.assertEqual(len(set(norms)), 5)
        for norm in norms:
            self.assertGreaterEqual(len(norm), 3)
            self.assertLessEqual(len(norm), 20)
            self.assertRegex(norm, r"^[a-z0-9_]+$")

    def test_operation_bounds(self):
        total = sum(len(t["ops"]) for t in self.traces.values())
        self.assertLessEqual(total, self.suite["trace"]["max_total_ops"])
        for name, trace in self.traces.items():
            self.assertLessEqual(len(trace["ops"]),
                                 self.suite["trace"]["max_ops_per_demo"],
                                 name)
            self.assertGreater(len(trace["ops"]), 0, name)

    def test_deterministic_hashes_and_identities(self):
        again = DEMOS.build_traces(tuple(self.suite["display_names"]))
        self.assertEqual(DEMOS.manifest_digest(self.traces),
                         DEMOS.manifest_digest(again))
        for name in self.traces:
            self.assertEqual(self.traces[name]["trace_hash"],
                             again[name]["trace_hash"], name)
            self.assertEqual(self.traces[name]["game_uuid"],
                             again[name]["game_uuid"], name)

    def test_every_demo_has_nonzero_xp_in_every_skill(self):
        for name, trace in self.traces.items():
            xp = trace["expected"]["xp_micro"]
            for skill in ("mining", "woodcutting", "smithing", "crafting",
                          "fishing", "cooking"):
                self.assertGreater(int(xp.get(skill, 0)), 0,
                                   f"{name}/{skill}")

    def test_reproducible_tie_in_cooking(self):
        left = self.traces["DemoFlint"]["expected"]["xp_micro"]["cooking"]
        right = self.traces["DemoMoss"]["expected"]["xp_micro"]["cooking"]
        self.assertEqual(left, right)
        self.assertGreater(int(left), 0)

    def test_manifest_and_reserved_emails(self):
        digest = DEMOS.manifest_digest(self.traces)
        self.assertEqual(self.suite["manifest_digest"], digest)
        self.assertEqual(self.suite["label"], "Demo")
        for name, trace in self.traces.items():
            self.assertTrue(trace["email"].endswith(".example.invalid"))
            self.assertEqual(self.suite["players"][name]["trace_hash"],
                             trace["trace_hash"])
            self.assertEqual(self.suite["players"][name]["email"],
                             trace["email"])

    def test_no_credentials_or_retired_names(self):
        def _walk(value, path="suite"):
            if isinstance(value, dict):
                for key, child in value.items():
                    self.assertNotIn(str(key).lower(), FORBIDDEN_KEYS,
                                     f"credential field at {path}.{key}")
                    _walk(child, f"{path}.{key}")
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    _walk(child, f"{path}[{index}]")
        _walk(self.suite)
        self.assertFalse(set(self.traces) & set(
            HISTORICAL.load_suite()["display_names"]))


if __name__ == "__main__":
    unittest.main()
