# tests/test_endurance_metrics.py - Endurance statistics falsification tests.
"""The old endurance code unpacked samples as tuples and reported a zero
slope when it had too few samples. These tests exercise dictionary-shaped
samples, insufficient data, window selection, monotonicity, slope/settled
limits and invalid values without waiting for a real 30-minute run.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load(name: str, relative: str):
    path = os.path.join(ROOT, relative)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


END = _load("ankiscape_endurance_metrics_under_test", "dev/endurance_metrics.py")


def _series(minutes: float, rss_at=lambda minute: 100.0, cadence_s: int = 30):
    samples = []
    t = 0.0
    while t <= minutes * 60.0:
        samples.append({"at_s": t, "rss_mib": rss_at(t / 60.0),
                        "objects": 1000, "threads": 4, "answers": int(t)})
        t += cadence_s
    return samples


class EnduranceMetricsTests(unittest.TestCase):
    def test_dictionary_samples_do_not_crash(self):
        # This is the exact shape that used to raise ValueError on unpack.
        report = END.evaluate_endurance(
            [{"at_s": 0.0, "rss_mib": 100.0}, {"at_s": 30.0, "rss_mib": 101.0},
             {"at_s": 60.0, "rss_mib": 101.5}],
            profile="engine-smoke", duration_min=1.0)
        self.assertIsInstance(report["slope_mib_per_min"], float)
        self.assertNotEqual(report["slope_mib_per_min"], 0.0)

    def test_insufficient_samples_fail_not_zero(self):
        report = END.evaluate_endurance([{"at_s": 0.0, "rss_mib": 100.0}],
                                        profile="nightly", duration_min=30.0)
        self.assertFalse(report["pass"])
        self.assertIsNone(report["slope_mib_per_min"])
        self.assertTrue(any("insufficient" in f for f in report["failures"]),
                        report["failures"])

    def test_release_requires_warmup_plus_window(self):
        report = END.evaluate_endurance(_series(30), profile="release",
                                        duration_min=30.0)
        self.assertFalse(report["pass"])
        self.assertFalse(report["eligible_for_release"])
        self.assertTrue(any("release_window_required" in f
                            for f in report["failures"]), report["failures"])

    def test_release_measures_final_window_only(self):
        # A one-time bump before the final window must not count as growth.
        def rss(minute):
            return 100.0 + (50.0 if 10.0 <= minute <= 20.0 else 0.0)
        report = END.evaluate_endurance(_series(90, rss), profile="release",
                                        duration_min=90.0)
        self.assertTrue(report["eligible_for_release"])
        self.assertLess(report["settled_increase_mib"], 1.0)
        self.assertTrue(report["pass"], report["failures"])

    def test_growth_in_final_window_fails_slope(self):
        def rss(minute):
            return 100.0 + max(0.0, (minute - 60.0)) * 2.0
        report = END.evaluate_endurance(_series(90, rss), profile="release",
                                        duration_min=90.0)
        self.assertFalse(report["pass"])
        self.assertTrue(any("slope" in f for f in report["failures"]),
                        report["failures"])

    def test_settled_increase_uses_warm_baseline(self):
        def rss(minute):
            return 100.0 + (30.0 if minute >= 60.0 else 0.0)
        report = END.evaluate_endurance(_series(90, rss), profile="release",
                                        duration_min=90.0)
        self.assertAlmostEqual(report["settled_increase_mib"], 30.0, delta=1.0)
        self.assertTrue(report["pass"], report["failures"])

    def test_settled_limit_fails(self):
        def rss(minute):
            return 100.0 + (80.0 if minute >= 60.0 else 0.0)
        report = END.evaluate_endurance(_series(90, rss), profile="release",
                                        duration_min=90.0)
        self.assertFalse(report["pass"])
        self.assertTrue(any("settled" in f for f in report["failures"]),
                        report["failures"])

    def test_non_monotonic_time_fails(self):
        report = END.evaluate_endurance(
            [{"at_s": 0.0, "rss_mib": 100.0}, {"at_s": 60.0, "rss_mib": 101.0},
             {"at_s": 30.0, "rss_mib": 102.0}],
            profile="engine-smoke", duration_min=1.0)
        self.assertFalse(report["pass"])
        self.assertTrue(any("monotonic" in f for f in report["failures"]),
                        report["failures"])

    def test_invalid_values_fail(self):
        report = END.evaluate_endurance(
            [{"at_s": 0.0, "rss_mib": float("nan")},
             {"at_s": 30.0, "rss_mib": 100.0},
             {"at_s": 60.0, "rss_mib": -5.0}],
            profile="engine-smoke", duration_min=1.0)
        self.assertFalse(report["pass"])
        self.assertTrue(any("invalid" in f or "negative" in f
                            for f in report["failures"]), report["failures"])

    def test_non_dict_samples_fail(self):
        report = END.evaluate_endurance([(0.0, 100.0), (30.0, 101.0)],
                                        profile="engine-smoke", duration_min=1.0)
        self.assertFalse(report["pass"])
        self.assertTrue(any("not_dict" in f for f in report["failures"]),
                        report["failures"])

    def test_nightly_trend_marks_ineligible_for_release(self):
        report = END.evaluate_endurance(_series(30), profile="nightly",
                                        duration_min=30.0)
        self.assertTrue(report["pass"], report["failures"])
        self.assertFalse(report["eligible_for_release"])

    def test_growing_phase_is_not_a_leak(self):
        # The fixed-history lifecycle is flat; the paced growing history
        # climbs 200 MiB. Only the fixed phase may decide the verdict.
        samples = []
        for t in range(0, 900, 30):
            samples.append({"at_s": float(t), "rss_mib": 100.0 + t / 900.0 * 200,
                            "phase": "growing",
                            "answers": int(t * 2.0)})
        for t in range(900, 1800 + 1, 30):
            samples.append({"at_s": float(t), "rss_mib": 320.0,
                            "phase": "fixed",
                            "answers": int(t * 2.0)})
        report = END.evaluate_endurance(samples, profile="nightly",
                                        duration_min=30.0)
        self.assertEqual(report["evaluated_phase"], "fixed")
        self.assertAlmostEqual(report["slope_mib_per_min"], 0.0, places=3)
        self.assertLess(report["settled_increase_mib"], 1.0)
        self.assertGreater(report["growing_rss_change_mib"], 100.0)
        self.assertTrue(report["pass"], report["failures"])

    def test_fixed_phase_leak_still_fails(self):
        samples = []
        for t in range(0, 900, 30):
            samples.append({"at_s": float(t), "rss_mib": 100.0,
                            "phase": "growing",
                            "answers": int(t * 2.0)})
        for t in range(900, 1800 + 1, 30):
            samples.append({"at_s": float(t),
                            "rss_mib": 200.0 + (t - 900) / 60.0 * 2.0,
                            "phase": "fixed",
                            "answers": int(t * 2.0)})
        report = END.evaluate_endurance(samples, profile="nightly",
                                        duration_min=30.0)
        self.assertFalse(report["pass"])
        self.assertTrue(any("slope" in f for f in report["failures"]),
                        report["failures"])

    def test_trend_discards_cache_warmup_ramp(self):
        # Real shape from nightly 34641447537 (Linux 26.8.1): the fixed
        # phase ramps ~57 MiB in the first minutes, then plateaus. The
        # nightly trend must judge the plateau, not the ramp.
        samples = []
        for t in range(0, 900, 30):
            samples.append({"at_s": float(t), "rss_mib": 600.0,
                            "phase": "growing",
                            "answers": int(t * 2.0)})
        for t in range(900, 1800 + 1, 30):
            ramp = min(1.0, max(0.0, (t - 900) / 240.0))
            samples.append({"at_s": float(t), "rss_mib": 643.0 + ramp * 57.5,
                            "phase": "fixed",
                            "answers": int(t * 2.0)})
        report = END.evaluate_endurance(samples, profile="nightly",
                                        duration_min=30.0)
        self.assertEqual(report["evaluated_phase"], "fixed")
        self.assertGreater(report["trend_warmup_dropped"], 0)
        self.assertTrue(report["pass"], report["failures"])

    def test_short_fixed_trend_fails(self):
        samples = []
        for t in range(0, 840, 30):
            samples.append({"at_s": float(t), "rss_mib": 100.0,
                            "phase": "growing",
                            "answers": int(t * 2.0)})
        for t in range(840, 1200 + 1, 30):
            samples.append({"at_s": float(t), "rss_mib": 100.0,
                            "phase": "fixed",
                            "answers": int(t * 2.0)})
        report = END.evaluate_endurance(samples, profile="nightly",
                                        duration_min=30.0)
        self.assertTrue(any("trend_fixed_span" in f
                            for f in report["failures"]), report["failures"])

    def test_zero_answer_run_fails_instead_of_reporting_green(self):
        # The endurance deck held 8 cards while the scenario meant to answer
        # ~14,400 (2/s for 120 min). The queue emptied, Anki showed
        # "finished this deck", the driver idled outside the reviewer -- and
        # the memory trend still looked perfectly flat and PASSED, because
        # nothing asserted that any review had happened. A trend run that
        # answered nothing must fail loudly: it measured an idle app.
        samples = []
        for t in range(0, 7200 + 1, 30):
            samples.append({"at_s": float(t), "rss_mib": 400.0,
                            "phase": "fixed", "answers": 0})
        report = END.evaluate_endurance(samples, profile="release",
                                        duration_min=120.0)
        self.assertFalse(report["pass"])
        self.assertIn("no_answers_measured", report["failures"])
        self.assertEqual(report["answers"], 0)

    def test_answering_run_reports_its_answer_count(self):
        # Companion to the guard above: the count must be surfaced, so a
        # reviewer can tell a loaded run from an idle one without reading
        # the raw samples. `_series` answers 1/s, so 30 min ends at 1800.
        report = END.evaluate_endurance(_series(30), profile="nightly",
                                        duration_min=30.0)
        self.assertEqual(report["answers"], 1800)
        self.assertNotIn("no_answers_measured", report["failures"])

    def test_falling_memory_is_not_a_leak(self):
        # The sampler used getrusage().ru_maxrss, a HIGH-WATER MARK that can
        # never decrease -- so a negative slope was impossible to observe and
        # this shape had no coverage. Current RSS really does fall (measured:
        # -282 MiB across one 8-minute run), and a falling series must pass.
        samples = []
        for t in range(0, 1800 + 1, 30):
            samples.append({"at_s": float(t),
                            "rss_mib": 900.0 - (t / 1800.0) * 300.0,
                            "phase": "fixed", "answers": int(t * 2.0)})
        report = END.evaluate_endurance(samples, profile="nightly",
                                        duration_min=30.0)
        self.assertLess(report["slope_mib_per_min"], 0)
        self.assertFalse(any("slope" in f for f in report["failures"]),
                         report["failures"])
        self.assertFalse(any("settled" in f for f in report["failures"]),
                         report["failures"])

    def test_engine_smoke_labels_ineligible(self):
        report = END.evaluate_endurance(_series(1), profile="engine-smoke",
                                        duration_min=1.0)
        self.assertFalse(report["eligible_for_release"])


if __name__ == "__main__":
    unittest.main()
