# tests/test_reliability_fail_fast.py - a red release lane skips its endurance run.
"""The release lane's two-hour endurance run is about two thirds of the lane and
runs last. The validator fails a lane for any scenario that is not `pass`, so an
earlier failure already decides the lane; running endurance afterwards only
delays the verdict by two hours. These tests pin the skip, and that it can never
turn a lane green or hide the skipped scenario."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relative))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REL = _load("ankiscape_reliability_fail_fast", "dev/reliability.py")
RELEASE_ORDER = ["native-matrix", "native-journeys", "native-performance",
                 "endurance-2h"]


def _entry(sid, status):
    return {"id": sid, "status": status, "command": "", "exit_status": 0,
            "detail": "", "assertions": [{"name": sid, "ok": status == "pass"}],
            "counts": {}, "files": []}


class FailedBeforeTests(unittest.TestCase):
    def test_release_endurance_is_skipped_after_any_failure(self):
        results = [_entry("native-matrix", "pass"), _entry("native-journeys", "fail")]
        self.assertEqual(
            REL._failed_before("release", {"id": "endurance-2h"}, results),
            ["native-journeys"])

    def test_a_clean_lane_runs_endurance(self):
        results = [_entry(s, "pass") for s in RELEASE_ORDER[:3]]
        self.assertEqual(
            REL._failed_before("release", {"id": "endurance-2h"}, results), [])

    def test_only_the_release_endurance_run_is_ever_skipped(self):
        failed = [_entry("native-matrix", "fail")]
        for stage, sid in (("release", "native-journeys"),
                           ("release", "native-performance"),
                           ("nightly", "endurance-30m"),
                           ("nightly", "native-performance")):
            self.assertEqual(REL._failed_before(stage, {"id": sid}, failed), [],
                             f"{stage}/{sid}")

    def test_a_skipped_scenario_is_never_a_pass(self):
        entry = REL._skipped_entry({"id": "endurance-2h"}, ["native-journeys"])
        self.assertEqual(entry["status"], "skipped")
        self.assertNotEqual(entry["exit_status"], 0)
        self.assertIn("native-journeys", entry["detail"])


class RunLaneTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = REL.run_scenario
        self.ran = []

    def tearDown(self):
        REL.run_scenario = self._orig
        self._tmp.cleanup()

    def _run_lane(self, outcomes, keep_going=False):
        def fake(ctx, scenario, stage, out_dir):
            self.ran.append(scenario["id"])
            return _entry(scenario["id"], outcomes.get(scenario["id"], "pass"))

        REL.run_scenario = fake
        out = os.path.join(self._tmp.name, "lane")
        args = argparse.Namespace(
            stage="release", role="native", out=out, matrix=REL.DEFAULT_MATRIX,
            os="linux", anki="26.8.1", anki_actual="", anki_bin="/x/Anki",
            qt="6", qt_actual="", trusted=False, native_timeout=7200,
            keep_going=keep_going)
        rc = REL.cmd_run_lane(args)
        with open(os.path.join(out, "record.json"), encoding="utf-8") as fh:
            return rc, json.load(fh)

    def test_failed_journeys_skip_endurance_and_the_lane_stays_red(self):
        rc, record = self._run_lane({"native-journeys": "fail"})
        self.assertEqual(self.ran, RELEASE_ORDER[:3])
        self.assertEqual(rc, 1)
        self.assertEqual(record["exit_status"], 1)
        by_id = {s["id"]: s for s in record["scenarios"]}
        self.assertEqual(list(by_id), RELEASE_ORDER)
        self.assertEqual(by_id["endurance-2h"]["status"], "skipped")

    def test_a_passing_lane_still_runs_endurance(self):
        rc, record = self._run_lane({})
        self.assertEqual(self.ran, RELEASE_ORDER)
        self.assertEqual(rc, 0)
        self.assertEqual({s["status"] for s in record["scenarios"]}, {"pass"})

    def test_keep_going_runs_endurance_after_a_failure(self):
        rc, record = self._run_lane({"native-performance": "fail"}, keep_going=True)
        self.assertEqual(self.ran, RELEASE_ORDER)
        self.assertEqual(rc, 1)

    def test_the_validator_still_fails_a_lane_with_a_skipped_scenario(self):
        _rc, record = self._run_lane({"native-journeys": "fail"})
        scenarios = {s["id"]: s for s in record["scenarios"]}
        self.assertNotEqual(scenarios["endurance-2h"]["status"], "pass")
        self.assertEqual(record["completed"], True)
        self.assertEqual(record["exit_status"], 1)


if __name__ == "__main__":
    unittest.main()
