# tests/test_fatal_retry.py - a journey whose only failed step is `fatal`.
"""Nightly 39984018007 (2026-10-23, macos-26.8.1-qt6, ui-onboarding) failed on
a single driver step named `fatal`: a "TypeError: Failed to fetch" alert in
Anki's webview became a Qt QMessageBox and blocked the main thread, so the
watchdog wrote the journeyless verdict and Anki exited -13. `_fail()` in
dev/e2e/driver_addon/__init__.py writes that step only from the harness itself
(watchdog, driver exception, unknown journey phase, classic_* check), never
from a product assertion, which is an ordinary named step. A journey whose
only failed step is `fatal` therefore relaunches once, as the teardown
relaunch does; a failed product step is never retried, and so is a second
`fatal`."""
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


DEV = _load("ankiscape_dev_fatal_retry", "dev.py")
UIX = _load("ankiscape_ui_ux_verify_fatal_retry", "dev/ui_ux_verify.py")
REL = _load("ankiscape_reliability_fatal_retry", "dev/reliability.py")

TEARDOWN = DEV.E2E_TEARDOWN_FAILED
JOURNEY = "ui-onboarding"
BOOT_OK = {"name": "boot", "ok": True}
FATAL = {"name": "fatal", "ok": False,
         "detail": "watchdog: journey did not finish in time"}
PASSING = [BOOT_OK, {"name": "onboarding-shell", "ok": True}]


class FakeDev:
    E2E_TEARDOWN_FAILED = TEARDOWN

    def __init__(self, dev_dir, script):
        self.DEV_DIR = dev_dir
        self.script = list(script)
        self.calls = 0

    def _e2e_suite(self, anki, qt, journey, anki_bin, anki_actual):
        """Each script entry is one launch: {"rc": ..., "steps": [...]}.
        `steps` omitted means Anki died before the driver wrote a result."""
        launch = self.script[self.calls]
        self.calls += 1
        base = os.path.join(self.DEV_DIR, "e2e", journey)
        shutil.rmtree(base, ignore_errors=True)  # the real suite wipes it
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "anki-stdout.log"), "w",
                  encoding="utf-8") as fh:
            fh.write(f"launch {self.calls}\n")
        with open(os.path.join(base, "run-id.txt"), "w", encoding="utf-8") as fh:
            fh.write(f"run-{self.calls}")
        if launch.get("steps") is not None:
            with open(os.path.join(base, "e2e-assertions.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"journey": journey, "run_id": f"run-{self.calls}",
                           "steps": launch["steps"],
                           "runtime": {"anki": "26.08.1", "qt": "6.9.1",
                                       "python": "3.13", "arch": "arm64"}}, fh)
        return launch["rc"]


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dev_dir = os.path.join(self._tmp.name, "dev")
        self.out_dir = os.path.join(self._tmp.name, "out")
        os.makedirs(self.out_dir)

    def tearDown(self):
        self._tmp.cleanup()


class MatrixFatalRetryTests(_TmpCase):
    def setUp(self):
        super().setUp()
        self._orig = UIX._settle_before_retry
        UIX._settle_before_retry = lambda: None

    def tearDown(self):
        UIX._settle_before_retry = self._orig
        super().tearDown()

    def run_journey(self, script):
        fake = FakeDev(self.dev_dir, script)
        args = type("Args", (), {"anki": "26.08.1", "qt": "6",
                                 "anki_bin": "/x", "anki_actual": ""})()
        result = UIX._run_journey_with_retries(fake, args, JOURNEY,
                                               self.out_dir)
        return fake, result

    def test_fatal_only_failure_is_relaunched_once_and_keeps_attempt_one(self):
        fake, (rc, assertions, expected, teardown, fatal) = self.run_journey(
            [{"rc": 1, "steps": [BOOT_OK, FATAL]},
             {"rc": 0, "steps": PASSING}])
        self.assertEqual((fake.calls, rc, teardown, fatal), (2, 0, 0, 1))
        self.assertEqual(assertions["run_id"], expected)
        kept = os.path.join(self.out_dir, f"{JOURNEY}-attempt1",
                            "anki-stdout.log")
        with open(kept, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "launch 1\n")
        with open(os.path.join(self.out_dir, f"{JOURNEY}-attempt1",
                               "e2e-assertions.json"), encoding="utf-8") as fh:
            self.assertEqual([s["name"] for s in json.load(fh)["steps"]],
                             ["boot", "fatal"])

    def test_fatal_beside_a_failed_product_step_is_not_relaunched(self):
        steps = [BOOT_OK, {"name": "onboarding-shell", "ok": False}, FATAL]
        fake, (rc, _a, _r, teardown, fatal) = self.run_journey(
            [{"rc": 1, "steps": steps}, {"rc": 0, "steps": PASSING}])
        self.assertEqual((fake.calls, rc, teardown, fatal), (1, 1, 0, 0))
        self.assertFalse(os.path.isdir(
            os.path.join(self.out_dir, f"{JOURNEY}-attempt1")))

    def test_a_failed_product_step_alone_is_not_relaunched(self):
        fake, (rc, _a, _r, teardown, fatal) = self.run_journey(
            [{"rc": 1, "steps": [BOOT_OK,
                                 {"name": "onboarding-shell", "ok": False}]},
             {"rc": 0, "steps": PASSING}])
        self.assertEqual((fake.calls, rc, teardown, fatal), (1, 1, 0, 0))

    def test_second_fatal_failure_fails_the_journey(self):
        fake, (rc, assertions, expected, teardown, fatal) = self.run_journey(
            [{"rc": 1, "steps": [FATAL]}, {"rc": 1, "steps": [FATAL]},
             {"rc": 0, "steps": PASSING}])
        self.assertEqual((fake.calls, rc, teardown, fatal), (2, 1, 0, 1))
        self.assertEqual(assertions["run_id"], expected)
        self.assertEqual([s["name"] for s in assertions["steps"]],
                         ["fatal"])

    def test_a_fatal_relaunch_is_not_retried_again_as_a_teardown_failure(self):
        fake, (rc, _a, _r, teardown, fatal) = self.run_journey(
            [{"rc": 1, "steps": [FATAL]},
             {"rc": TEARDOWN, "steps": PASSING},
             {"rc": 0, "steps": PASSING}])
        self.assertEqual((fake.calls, rc, teardown, fatal),
                         (2, TEARDOWN, 0, 1))

    def test_teardown_retry_path_is_unchanged(self):
        fake, (rc, _a, _r, teardown, fatal) = self.run_journey(
            [{"rc": TEARDOWN, "steps": PASSING},
             {"rc": 0, "steps": PASSING}])
        self.assertEqual((fake.calls, rc, teardown, fatal), (2, 0, 1, 0))
        self.assertTrue(os.path.isdir(
            os.path.join(self.out_dir, f"{JOURNEY}-attempt1")))

    def test_launch_race_then_fatal_retry_each_run_once(self):
        fake, (rc, _a, _r, teardown, fatal) = self.run_journey(
            [{"rc": -13}, {"rc": 1, "steps": [FATAL]},
             {"rc": 0, "steps": PASSING}])
        self.assertEqual((fake.calls, rc, teardown, fatal), (3, 0, 0, 1))


class ResultsJsonTests(unittest.TestCase):
    def test_observed_runtime_carries_fatal_retries_next_to_teardown(self):
        args = type("Args", (), {"anki": "26.08.1", "qt": 6})()
        results = [
            {"journey": JOURNEY, "teardown_retries": 0, "fatal_retries": 1},
            {"journey": "ui-training", "teardown_retries": 1, "fatal_retries": 0},
            {"journey": "ui-review", "teardown_retries": 0, "fatal_retries": 0},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "observed-runtime.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(UIX._observed_runtime(args, results,
                                                {"anki": "26.08.1"}), fh)
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        self.assertEqual(payload["fatal_retries"], {JOURNEY: 1})
        self.assertEqual(payload["teardown_retries"], {"ui-training": 1})
        self.assertEqual(payload["scenarios"], 3)


class FatalWarningTests(unittest.TestCase):
    def test_reader_counts_come_from_a_map_or_journey_entries(self):
        self.assertEqual(REL._fatal_retries(
            {"fatal_retries": {JOURNEY: 1, "ui-x": 0}}), {JOURNEY: 1})
        self.assertEqual(REL._fatal_retries(
            {"journeys": [{"journey": JOURNEY, "fatal_retries": 1},
                          {"journey": "ui-recovery", "fatal_retries": 0}]}),
            {JOURNEY: 1})
        self.assertEqual(REL._fatal_retries({}), {})
        self.assertEqual(REL._fatal_retries(
            {"fatal_retries": {"ui-art": True}}), {})

    def test_evidence_emits_the_fatal_retried_warning(self):
        warnings = REL._retry_warnings(
            {"id": "native-matrix", "fatal_retries": {JOURNEY: 1}},
            "macos-26.8.1-qt6/native-matrix.json")
        self.assertEqual(
            warnings, ["fatal_retried:native-matrix:ui-onboarding:x1:"
                       "macos-26.8.1-qt6/native-matrix.json"])

    def test_teardown_warning_shape_is_unchanged(self):
        warnings = REL._retry_warnings(
            {"id": "native-matrix", "teardown_retries": {JOURNEY: 1}},
            "macos-26.8.1-qt6/native-matrix.json")
        self.assertEqual(
            warnings, ["teardown_retried:native-matrix:ui-onboarding:x1:"
                       "macos-26.8.1-qt6/native-matrix.json"])


class NativeMatrixFatalPassThroughTests(unittest.TestCase):
    def test_observed_runtime_fatal_retries_reach_the_scenario_record(self):
        with tempfile.TemporaryDirectory() as root:
            orig = (REL.ROOT, REL._run_command, REL._load_dev_module)
            try:
                REL.ROOT = root
                os.makedirs(os.path.join(root, "dev"))
                with open(os.path.join(root, "dev", "ui_ux_verify.py"), "w",
                          encoding="utf-8") as fh:
                    fh.write("")
                ui_ux = os.path.join(root, "artifacts", "ui-ux")
                os.makedirs(ui_ux)
                with open(os.path.join(
                        ui_ux, "observed-runtime-26.08.1-qt6.json"), "w",
                        encoding="utf-8") as fh:
                    json.dump({"requested": {"anki": "26.08.1", "qt": 6},
                               "observed": {"anki": "26.08.1", "qt": "6.9.1"},
                               "scenarios": 22,
                               "teardown_retries": {},
                               "fatal_retries": {JOURNEY: 1}}, fh)
                REL._run_command = lambda *a, **k: (0, "")
                REL._load_dev_module = lambda: type(
                    "FakeDevModule", (), {"DEV_DIR": os.path.join(root, "dev")})()
                out_dir = os.path.join(root, "out")
                os.makedirs(out_dir)
                result = REL._run_native_matrix(
                    {"anki": "26.08.1", "qt": 6, "anki_bin": "/x/Anki"},
                    out_dir)
            finally:
                REL.ROOT, REL._run_command, REL._load_dev_module = orig
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["fatal_retries"], {JOURNEY: 1})
        self.assertNotIn("teardown_retries", result)


if __name__ == "__main__":
    unittest.main()
