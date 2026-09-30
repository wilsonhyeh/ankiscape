# tests/test_reliability_launch_retry.py - native journeys relaunch a dead Anki.
"""The native-journeys and native-smoke scenarios run journeys through
`_e2e_journey_result`. Anki that exits before the driver writes any result
asserted nothing, so it is launched once more; a run that wrote assertions is
never retried, so a failed step cannot be retried away."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
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


REL = _load("ankiscape_reliability_launch_retry", "dev/reliability.py")
JOURNEY = "ui-recovery"


class FakeDev:
    """dev.py stand-in: each entry of `script` is one launch of the journey."""

    def __init__(self, dev_dir, script):
        self.DEV_DIR = dev_dir
        self.script = list(script)
        self.calls = 0

    def _e2e_suite(self, anki, qt, journey, anki_bin, anki_actual):
        launch = self.script[self.calls]
        self.calls += 1
        base = os.path.join(self.DEV_DIR, "e2e", journey)
        if launch.get("wipes", True):  # the real suite wipes its base on launch
            shutil.rmtree(base, ignore_errors=True)
            os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "anki-stdout.log"), "w",
                  encoding="utf-8") as fh:
            fh.write(launch.get("stdout", "Starting Anki\n"))
        if launch.get("steps") is not None:
            with open(os.path.join(base, "e2e-assertions.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"steps": launch["steps"],
                           "runtime": {"anki": "26.08.1", "qt": "6.9"}}, fh)
        return launch["rc"]


class LaunchRetryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dev_dir = os.path.join(self._tmp.name, "dev")
        self.out_dir = os.path.join(self._tmp.name, "out")
        os.makedirs(self.out_dir)
        self._orig = (REL._load_dev_module, REL._settle_before_launch_retry)
        REL._settle_before_launch_retry = lambda: None

    def tearDown(self):
        REL._load_dev_module, REL._settle_before_launch_retry = self._orig
        self._tmp.cleanup()

    def _run(self, script, scenario_id="native-journeys"):
        fake = FakeDev(self.dev_dir, script)
        REL._load_dev_module = lambda: fake
        entry = REL._e2e_journey_result({"anki": "26.08.1", "qt": "6"},
                                        JOURNEY, self.out_dir,
                                        scenario_id=scenario_id)
        return fake, entry

    def test_clean_run_is_launched_once(self):
        fake, entry = self._run([{"rc": 0, "steps": [{"name": "a", "ok": True}]}])
        self.assertEqual(fake.calls, 1)
        self.assertEqual(entry["launch_retries"], 0)
        self.assertEqual(entry["exit_status"], 0)

    def test_dead_anki_is_relaunched_and_the_second_result_counts(self):
        fake, entry = self._run([
            {"rc": 1, "stdout": "Starting main loop...\n"},      # -13, no result
            {"rc": 0, "steps": [{"name": "a", "ok": True}]}])
        self.assertEqual(fake.calls, 2)
        self.assertEqual(entry["launch_retries"], 1)
        self.assertEqual(entry["exit_status"], 0)
        self.assertEqual(entry["failed"], [])
        self.assertEqual([a["name"] for a in entry["assertions"]], ["a"])

    def test_first_attempt_evidence_is_kept(self):
        _fake, entry = self._run([
            {"rc": 1, "stdout": "attempt one died here\n"},
            {"rc": 0, "steps": [{"name": "a", "ok": True}],
             "stdout": "attempt two\n"}])
        kept = os.path.join(
            self.out_dir, f"native-journeys-{JOURNEY}-attempt1-anki-stdout-tail.log")
        self.assertTrue(os.path.isfile(kept))
        with open(kept, encoding="utf-8") as fh:
            self.assertIn("attempt one died here", fh.read())
        names = [f["path"] for f in entry["files"]]
        self.assertIn(os.path.basename(kept), names)
        self.assertIn(f"native-journeys-{JOURNEY}-anki-stdout-tail.log", names)

    def test_a_failed_step_is_never_retried(self):
        fake, entry = self._run([
            {"rc": 1, "steps": [{"name": "a", "ok": True},
                                {"name": "b", "ok": False}]},
            {"rc": 0, "steps": [{"name": "a", "ok": True},
                                {"name": "b", "ok": True}]}])
        self.assertEqual(fake.calls, 1)
        self.assertEqual(entry["launch_retries"], 0)
        self.assertEqual(entry["exit_status"], 1)
        self.assertEqual(entry["failed"], ["b"])

    def test_relaunches_once_and_then_reports_the_failure(self):
        fake, entry = self._run([{"rc": 1}, {"rc": 1}, {"rc": 0, "steps": [
            {"name": "a", "ok": True}]}])
        self.assertEqual(fake.calls, 2)
        self.assertEqual(entry["launch_retries"], 1)
        self.assertEqual(entry["exit_status"], 1)
        self.assertEqual(entry["steps"], 0)

    def test_a_leftover_result_is_not_this_runs_evidence(self):
        base = os.path.join(self.dev_dir, "e2e", JOURNEY)
        os.makedirs(base)
        with open(os.path.join(base, "e2e-assertions.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"steps": [{"name": "old", "ok": True}]}, fh)
        # Both launches bail before wiping the base (e.g. a leftover Anki).
        fake, entry = self._run([{"rc": 1, "wipes": False},
                                 {"rc": 1, "wipes": False}])
        self.assertEqual(fake.calls, 2)
        self.assertEqual(entry["steps"], 0)
        self.assertEqual(entry["assertions"], [])
        self.assertEqual(entry["exit_status"], 1)

    def test_scenario_detail_names_the_relaunch(self):
        fake = FakeDev(self.dev_dir, [
            {"rc": 1}, {"rc": 0, "steps": [{"name": "a", "ok": True}]}])
        REL._load_dev_module = lambda: fake
        orig = REL.NEW_JOURNEYS
        REL.NEW_JOURNEYS = (JOURNEY,)
        try:
            result = REL._run_native_journeys(
                {"anki": "26.08.1", "qt": "6", "anki_bin": "/x/Anki"},
                self.out_dir)
        finally:
            REL.NEW_JOURNEYS = orig
        self.assertEqual(result["status"], "pass")
        self.assertIn("relaunched x1", result["detail"])


if __name__ == "__main__":
    unittest.main()
