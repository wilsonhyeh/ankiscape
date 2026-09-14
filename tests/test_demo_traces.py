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


demo_traces = _load("ankiscape_demo_traces", "dev/demo_traces.py")


class TestDemoTraces(unittest.TestCase):
    def test_exactly_five_named_demos(self):
        self.assertEqual(demo_traces.DISPLAY_NAMES,
                         ("DemoWillow", "DemoFlint", "DemoMoss",
                          "DemoRowan", "DemoCopper"))
        traces = demo_traces.build_traces()
        self.assertEqual(sorted(traces), sorted(demo_traces.DISPLAY_NAMES))

    def test_every_demo_has_nonzero_xp_in_every_skill(self):
        traces = demo_traces.build_traces()
        for name, trace in traces.items():
            xp = trace["expected"]["xp_micro"]
            for skill in ("mining", "woodcutting", "smithing", "crafting",
                          "fishing", "cooking"):
                self.assertGreater(int(xp.get(skill, 0)), 0,
                                   msg=f"{name}/{skill}")

    def test_reproducible_tie_and_varied_scores(self):
        traces = demo_traces.build_traces()
        left = traces["DemoFlint"]["expected"]["xp_micro"]["cooking"]
        right = traces["DemoMoss"]["expected"]["xp_micro"]["cooking"]
        self.assertEqual(left, right)
        totals = {name: sum(int(v) for v in t["expected"]["xp_micro"].values())
                  for name, t in traces.items()}
        self.assertGreater(len(set(totals.values())), 1,
                           "scores must vary across the population")
        # The tie is a tie, not the whole-board ordering: another skill differs.
        self.assertNotEqual(
            traces["DemoFlint"]["expected"]["xp_micro"]["mining"],
            traces["DemoMoss"]["expected"]["xp_micro"]["mining"])

    def test_deterministic_builds(self):
        first = demo_traces.build_traces()
        second = demo_traces.build_traces()
        self.assertEqual(demo_traces.manifest_digest(first),
                         demo_traces.manifest_digest(second))
        for name in first:
            self.assertEqual(first[name]["trace_hash"],
                             second[name]["trace_hash"])
            self.assertEqual(first[name]["game_uuid"],
                             second[name]["game_uuid"])

    def test_operation_bounds(self):
        traces = demo_traces.build_traces()
        total = 0
        for trace in traces.values():
            self.assertLessEqual(len(trace["ops"]),
                                 demo_traces.MAX_OPS_PER_DEMO)
            total += len(trace["ops"])
        self.assertLessEqual(total, demo_traces.MAX_TOTAL_OPS)

    def test_identities_are_reserved_and_valid(self):
        import re
        traces = demo_traces.build_traces()
        for name, trace in traces.items():
            self.assertTrue(trace["email"].endswith(".example.invalid"))
            self.assertTrue(re.match(r"^[a-z0-9_]{3,20}$", name.lower()))
            self.assertEqual(trace["username_norm"], name.lower())
            self.assertEqual(demo_traces.email_for(name),
                             f"{name.lower()}@public-demo.example.invalid")

    def test_checked_in_manifest_matches_generator(self):
        path = os.path.join("dev", "fixtures", "public-demo-v1.json")
        with open(path, encoding="utf-8") as fh:
            fixture = json.load(fh)
        traces = demo_traces.build_traces()
        self.assertEqual(fixture["manifest_digest"],
                         demo_traces.manifest_digest(traces))
        for name, trace in traces.items():
            entry = fixture["players"][name]
            self.assertEqual(entry["trace_hash"], trace["trace_hash"])
            self.assertEqual(entry["game_uuid"], trace["game_uuid"])
            self.assertEqual(entry["email"], trace["email"])
            self.assertEqual(entry["expected"]["xp_micro"],
                             trace["expected"]["xp_micro"])

    def test_no_hosted_v1_identities(self):
        traces = demo_traces.build_traces()
        for name in traces:
            self.assertTrue(name.startswith("Demo"))
        names = set(traces)
        legacy = {"WillowMere", "FlintHarbor", "Mosswarden", "RowanVale",
                  "CopperFinch"}
        self.assertFalse(names & legacy)


if __name__ == "__main__":
    unittest.main()
