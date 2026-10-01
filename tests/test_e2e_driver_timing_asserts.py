# tests/test_e2e_driver_timing_asserts.py - two timing assertions that flaked in release-verify.
"""release-verify attempt 4 lost two lanes to timing assertions in the e2e
journeys, not to the add-on:

* `ui-recovery` / `recovery_warning_shown` checked the warning flag on the first
  tick after an answer that Anki applies asynchronously (Linux Qt5 runner).
* `ui-rebuild-review` / `rebuild_answer_responsive` judged one answer's time
  against 250 ms while a 100k-op rebuild publishes: 313.6 ms in attempt 1 and
  315.5 ms in attempt 4 on macOS, against 1 to 2 ms on every Linux and Windows
  lane. Product budgets allow main-thread stalls of 5000 ms while a rebuild
  publishes, and the performance gate judges them with real sample counts.

These tests pin the waits and the median, and that the budget itself is unchanged."""
from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRIVER = os.path.join(ROOT, "dev", "e2e", "driver_addon", "__init__.py")


def _load_driver(path=None):
    spec = importlib.util.spec_from_file_location("e2e_driver_timing_under_test",
                                                  path or DRIVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AnswerLatencyVerdictTests(unittest.TestCase):
    def setUp(self):
        self.drv = _load_driver()

    def test_the_budget_is_still_250_ms(self):
        self.assertEqual(self.drv.REBUILD_ANSWER_BUDGET_MS, 250.0)

    def test_one_stalled_answer_does_not_fail_the_check(self):
        # The macOS stall of attempt 4, on an otherwise fast journey.
        ok, detail = self.drv._answer_latency_verdict([1.2, 0.9, 315.5])
        self.assertTrue(ok, detail)
        self.assertIn("315.5", detail)         # every sample is reported
        self.assertIn("median=1.2ms", detail)

    def test_a_stall_on_the_first_or_second_answer_is_also_tolerated(self):
        self.assertTrue(self.drv._answer_latency_verdict([313.6, 1.0, 1.1])[0])
        self.assertTrue(self.drv._answer_latency_verdict([1.0, 313.6, 1.1])[0])

    def test_answers_that_are_slow_every_time_still_fail(self):
        ok, detail = self.drv._answer_latency_verdict([310.0, 312.0, 316.0])
        self.assertFalse(ok, detail)

    def test_the_limit_is_strict(self):
        self.assertFalse(self.drv._answer_latency_verdict([250.0] * 3)[0])
        self.assertTrue(self.drv._answer_latency_verdict([249.9] * 3)[0])

    def test_two_slow_of_three_fails(self):
        self.assertFalse(self.drv._answer_latency_verdict([300.0, 1.0, 400.0])[0])

    def test_an_even_count_uses_the_mean_of_the_middle_pair(self):
        ok, detail = self.drv._answer_latency_verdict([1.0, 2.0, 3.0, 4.0])
        self.assertTrue(ok)
        self.assertIn("median=2.5ms", detail)

    def test_nothing_timed_fails_closed(self):
        ok, detail = self.drv._answer_latency_verdict([])
        self.assertFalse(ok)
        self.assertIn("no answer", detail)


class RecoveryWarningWaitTests(unittest.TestCase):
    """Drives the real `_poll_ui_recovery` verify_failure stage."""

    def setUp(self):
        self.warning = {"active": False, "message": ""}
        fake = types.ModuleType("ankiscape")
        fake._RECOVERY_WARNING = self.warning
        fake._EVOLVED_CTX = {"engine": None}
        patcher = mock.patch.dict(sys.modules, {"ankiscape": fake})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.drv = _load_driver()
        self.steps = []
        self.drv._step = lambda name, ok, detail="": self.steps.append((name, ok))
        self.drv._shot = lambda *a, **k: None
        self.drv._journal_ops = lambda: []
        self.state = {"stage": "verify_failure", "journal_before": 0,
                      "original_record": None}

    def tick(self, n=1):
        for _ in range(n):
            self.drv._poll_ui_recovery(self.state)

    def test_a_warning_that_arrives_late_is_waited_for(self):
        self.tick(3)                       # the answer has not been applied yet
        self.assertEqual(self.steps, [])
        self.assertEqual(self.state["stage"], "verify_failure")
        self.warning["active"] = True      # Anki applied it
        self.tick()
        self.assertEqual(self.steps, [("recovery_warning_shown", True),
                                      ("recovery_no_operation_persisted", True)])
        self.assertEqual(self.state["stage"], "recover")

    def test_a_warning_present_on_the_first_tick_is_recorded_at_once(self):
        self.warning["active"] = True
        self.tick()
        self.assertEqual(self.steps[0], ("recovery_warning_shown", True))
        self.assertEqual(self.state["stage"], "recover")

    def test_it_gives_up_at_the_bound_and_reports_the_failure(self):
        bound = self.drv.RECOVERY_WAIT_TICKS
        self.tick(bound)                   # still waiting through the bound
        self.assertEqual(self.steps, [])
        self.assertEqual(self.state["stage"], "verify_failure")
        self.tick()                        # the first tick past it records
        self.assertEqual(self.steps[0], ("recovery_warning_shown", False))
        self.assertEqual(self.state["stage"], "recover")

    def test_the_bound_matches_the_recovery_stage(self):
        self.assertGreaterEqual(self.drv.RECOVERY_WAIT_TICKS, 200)


if __name__ == "__main__":
    unittest.main()
