# tests/test_fixture_plans.py - Hosted fixture suite determinism and bounds.
"""Pure checks for dev/fixtures/hosted-v1.json and dev/fixture_traces.py:
24 fixed names, deterministic hashes/ids, operations within the per-player
and total bounds, tie/zero/gem/undo coverage, and no credentials anywhere."""
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


TRACES = _load("ankiscape_fixture_traces_test", "dev/fixture_traces.py")


class HostedSuiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite = TRACES.load_suite()
        cls.traces = TRACES.build_traces(cls.suite["display_names"])

    def test_exactly_twenty_four_unique_natural_names(self):
        names = self.suite["display_names"]
        self.assertEqual(len(names), 24)
        norms = [TRACES.username_norm(n) for n in names]
        self.assertEqual(len(set(norms)), 24)
        for norm in norms:
            self.assertGreaterEqual(len(norm), 3)
            self.assertLessEqual(len(norm), 20)
            self.assertRegex(norm, r"^[a-z0-9_]+$")

    def test_operation_bounds(self):
        total = sum(len(t["ops"]) for t in self.traces.values())
        self.assertLessEqual(total, self.suite["trace"]["max_total_ops"])
        for name, trace in self.traces.items():
            self.assertLessEqual(len(trace["ops"]),
                                 self.suite["trace"]["max_ops_per_player"],
                                 name)
            self.assertGreater(len(trace["ops"]), 0, name)

    def test_deterministic_hashes_and_identities(self):
        again = TRACES.build_traces(self.suite["display_names"])
        for name in self.traces:
            self.assertEqual(self.traces[name]["trace_hash"],
                             again[name]["trace_hash"], name)
            self.assertEqual(self.traces[name]["game_uuid"],
                             again[name]["game_uuid"], name)
            self.assertEqual(self.traces[name]["ops"][0]["op_id"],
                             again[name]["ops"][0]["op_id"], name)
            self.assertNotEqual(self.traces[name]["trace_hash"], "")

    def test_game_uuids_are_unique(self):
        games = [t["game_uuid"] for t in self.traces.values()]
        self.assertEqual(len(set(games)), 24)

    def test_tie_pair_has_identical_expected_xp(self):
        tie = [t for t in self.traces.values() if t["slot"] in (1, 2)]
        self.assertEqual(len(tie), 2)
        self.assertEqual(tie[0]["expected"]["xp_micro"],
                         tie[1]["expected"]["xp_micro"])
        self.assertEqual(tie[0]["expected"]["total_level"],
                         tie[1]["expected"]["total_level"])

    def test_zero_score_player_earns_no_xp(self):
        zero = [t for t in self.traces.values() if t["slot"] == 0][0]
        self.assertEqual(sum(zero["expected"]["xp_micro"].values()), 0)

    def test_gem_player_holds_a_mined_gem(self):
        gem = [t for t in self.traces.values() if t["slot"] == 5][0]
        gems = [k for k in gem["expected"]["inventory"]
                if k.startswith("Uncut ")]
        self.assertTrue(gems, "fixture gem trace produced no gem")

    def test_undo_player_has_retract_and_restore(self):
        undo = [t for t in self.traces.values() if t["slot"] == 6][0]
        kinds = [op["kind"] for op in undo["ops"]]
        self.assertIn("review_retract", kinds)
        self.assertIn("review_restore", kinds)
        # One award restored, one left retracted: exactly one skill earned.
        earned = [s for s, xp in undo["expected"]["xp_micro"].items() if xp > 0]
        self.assertEqual(len(earned), 1)

    def test_material_and_recipe_coverage_present(self):
        outcomes = set()
        for trace in self.traces.values():
            for op in trace["ops"]:
                if op["kind"] == "review_award":
                    outcomes.add(op["payload"].get("skill"))
                if op["kind"] == "review_skip":
                    outcomes.add("skip")
        for expected in ("mining", "woodcutting", "fishing", "cooking",
                         "smithing", "crafting", "skip"):
            self.assertIn(expected, outcomes)

    def test_no_credentials_or_secrets_in_suite(self):
        forbidden_keys = {"password", "secret", "service_role", "access_token",
                          "refresh_token", "anon_key", "apikey"}

        def _walk(value, path="suite"):
            if isinstance(value, dict):
                for key, child in value.items():
                    self.assertNotIn(str(key).lower(), forbidden_keys,
                                     f"credential field at {path}.{key}")
                    _walk(child, f"{path}.{key}")
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    _walk(child, f"{path}[{index}]")

        _walk(self.suite)
        # Display names never contain addresses or secret-looking material.
        for name in self.suite["display_names"]:
            self.assertNotIn("@", name)

    def test_lane_assignment_covers_all_names(self):
        lanes = self.suite["lanes"]
        assigned = [n for names in lanes.values() for n in names]
        self.assertEqual(sorted(assigned), sorted(self.suite["display_names"]))
        self.assertEqual(len(assigned), 24)


if __name__ == "__main__":
    unittest.main()
