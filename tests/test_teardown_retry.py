# tests/test_teardown_retry.py - a passing journey whose Anki crashes on quit.
"""On nightlies 2026-10-12..10-21 every crash/hang journey failure had passed
all its steps: Anki then segfaulted, hit an access violation or hung on quit.
`dev._e2e_suite` names that outcome (E2E_TEARDOWN_FAILED) and the matrix and
performance runners launch it once more. A failed step is never retried, and a
second teardown failure is still a failure."""
from __future__ import annotations

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


DEV = _load("ankiscape_dev_teardown_retry", "dev.py")
NP = _load("ankiscape_native_performance_teardown_retry",
           "dev/native_performance.py")
UIX = _load("ankiscape_ui_ux_verify_teardown_retry", "dev/ui_ux_verify.py")
REL = _load("ankiscape_reliability_teardown_retry", "dev/reliability.py")

TEARDOWN = DEV.E2E_TEARDOWN_FAILED
PASSING = {"journey": "ui-slow-sync", "screenshots": ["a.png"],
           "steps": [{"name": "a", "ok": True}, {"name": "b", "ok": True}]}


class ExitVerdictTests(unittest.TestCase):
    def verdict(self, result=None, *, journey="ui-slow-sync", hung=False,
                returncode=0):
        return DEV._e2e_exit_verdict(journey, result or PASSING, hung=hung,
                                     returncode=returncode)

    def test_clean_pass(self):
        self.assertEqual(self.verdict(), 0)

    def test_crash_after_passing_steps_is_a_teardown_failure(self):
        for code in (-11, -13, 3221225477):
            self.assertEqual(self.verdict(returncode=code), TEARDOWN, code)

    def test_hang_after_passing_steps_is_a_teardown_failure(self):
        self.assertEqual(self.verdict(hung=True, returncode=-9), TEARDOWN)

    def test_a_failed_step_is_a_plain_failure_even_with_a_crash(self):
        result = dict(PASSING, steps=[{"name": "a", "ok": False}])
        self.assertEqual(self.verdict(result), 1)
        self.assertEqual(self.verdict(result, returncode=-11), 1)
        self.assertEqual(self.verdict(result, hung=True), 1)

    def test_missing_screenshots_or_wrong_journey_is_a_plain_failure(self):
        self.assertEqual(self.verdict(dict(PASSING, screenshots=[]),
                                      returncode=-11), 1)
        self.assertEqual(self.verdict(dict(PASSING, journey="other"),
                                      returncode=-11), 1)

    def test_perf_journeys_need_no_screenshots(self):
        result = dict(PASSING, journey="native-performance", screenshots=[])
        self.assertEqual(self.verdict(result, journey="native-performance",
                                      returncode=-11), TEARDOWN)

    def test_teardown_code_is_distinct_from_failure_and_pass(self):
        self.assertNotIn(TEARDOWN, (0, 1, 2))


class FakeDev:
    E2E_TEARDOWN_FAILED = TEARDOWN

    def __init__(self, dev_dir, codes):
        self.DEV_DIR = dev_dir
        self.codes = list(codes)
        self.calls = 0

    def _e2e_suite(self, *args, **kwargs):
        code = self.codes[self.calls]
        self.calls += 1
        journey = args[2]
        base = os.path.join(self.DEV_DIR, "e2e", journey)
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "anki-stdout.log"), "w",
                  encoding="utf-8") as fh:
            fh.write(f"launch {self.calls}\n")
        with open(os.path.join(base, "e2e-assertions.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(dict(PASSING, journey=journey,
                           run_id=f"run-{self.calls}"), fh)
        with open(os.path.join(base, "run-id.txt"), "w", encoding="utf-8") as fh:
            fh.write(f"run-{self.calls}")
        with open(os.path.join(base, "e2e-perf-native.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"run_mode": "addon", "lag_ms": [1.0]}, fh)
        return code


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dev_dir = os.path.join(self._tmp.name, "dev")
        self.out_dir = os.path.join(self._tmp.name, "out")
        os.makedirs(self.out_dir)

    def tearDown(self):
        self._tmp.cleanup()


class PerformanceRepRetryTests(_TmpCase):
    def setUp(self):
        super().setUp()
        self._orig = dict(NP._NATIVE_MODULES)
        settle = type("Rel", (), {"_settle_before_launch_retry":
                                  staticmethod(lambda: None)})
        NP._NATIVE_MODULES["rel"] = settle

    def tearDown(self):
        NP._NATIVE_MODULES.clear()
        NP._NATIVE_MODULES.update(self._orig)
        super().tearDown()

    def run_rep(self, codes):
        fake = FakeDev(self.dev_dir, codes)
        NP._NATIVE_MODULES["dev"] = fake
        evidence = os.path.join(self.out_dir, "rep0-addon")
        run = NP._run_journey("native-performance", install_addon=True,
                              config={"answers": 1}, anki="26.08.1", qt="6",
                              anki_bin="/x", evidence_dir=evidence)
        return fake, run, evidence

    def test_clean_rep_runs_once(self):
        fake, run, _ = self.run_rep([0])
        self.assertEqual(fake.calls, 1)
        self.assertEqual(run["rc"], 0)
        self.assertEqual(run["teardown_retries"], 0)

    def test_teardown_failure_reruns_the_rep_and_keeps_attempt_one(self):
        fake, run, evidence = self.run_rep([TEARDOWN, 0])
        self.assertEqual(fake.calls, 2)
        self.assertEqual(run["rc"], 0)
        self.assertEqual(run["teardown_retries"], 1)
        self.assertTrue(os.path.isdir(evidence + "-attempt1"))

    def test_second_teardown_failure_fails_the_rep(self):
        fake, run, _ = self.run_rep([TEARDOWN, TEARDOWN, 0])
        self.assertEqual(fake.calls, 2)
        self.assertEqual(run["rc"], TEARDOWN)

    def test_a_plain_failure_is_not_rerun(self):
        fake, run, _ = self.run_rep([1, 0])
        self.assertEqual(fake.calls, 1)
        self.assertEqual(run["rc"], 1)
        self.assertEqual(run["teardown_retries"], 0)


class MatrixRetryTests(_TmpCase):
    def setUp(self):
        super().setUp()
        self._orig = UIX._settle_before_retry
        UIX._settle_before_retry = lambda: None

    def tearDown(self):
        UIX._settle_before_retry = self._orig
        super().tearDown()

    def run_journey(self, codes):
        fake = FakeDev(self.dev_dir, codes)
        args = type("Args", (), {"anki": "23.10", "qt": "6", "anki_bin": "/x",
                                 "anki_actual": ""})()
        result = UIX._run_journey_with_retries(fake, args, "ui-sync-stall",
                                               self.out_dir)
        return fake, result

    def test_clean_journey_runs_once(self):
        fake, (rc, _a, _r, retries) = self.run_journey([0])
        self.assertEqual((fake.calls, rc, retries), (1, 0, 0))

    def test_teardown_failure_is_retried_once_and_logs_are_kept(self):
        fake, (rc, assertions, expected, retries) = self.run_journey(
            [TEARDOWN, 0])
        self.assertEqual((fake.calls, rc, retries), (2, 0, 1))
        self.assertEqual(assertions["run_id"], expected)
        kept = os.path.join(self.out_dir, "ui-sync-stall-attempt1",
                            "anki-stdout.log")
        with open(kept, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "launch 1\n")

    def test_second_teardown_failure_is_reported(self):
        fake, (rc, _a, _r, retries) = self.run_journey([TEARDOWN, TEARDOWN, 0])
        self.assertEqual((fake.calls, rc, retries), (2, TEARDOWN, 1))

    def test_a_failed_journey_is_not_retried(self):
        fake, (rc, _a, _r, retries) = self.run_journey([1, 0])
        self.assertEqual((fake.calls, rc, retries), (1, 1, 0))


class TeardownWarningTests(unittest.TestCase):
    def test_counts_come_from_a_map_or_journey_entries(self):
        self.assertEqual(REL._teardown_retries(
            {"teardown_retries": {"ui-art": 1, "ui-x": 0}}), {"ui-art": 1})
        self.assertEqual(REL._teardown_retries(
            {"journeys": [{"journey": "ui-slow-sync", "teardown_retries": 1},
                          {"journey": "ui-recovery", "teardown_retries": 0}]}),
            {"ui-slow-sync": 1})
        self.assertEqual(REL._teardown_retries({}), {})
        self.assertEqual(REL._teardown_retries(
            {"teardown_retries": {"ui-art": True}}), {})


if __name__ == "__main__":
    unittest.main()
