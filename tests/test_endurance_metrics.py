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

    def test_engine_smoke_labels_ineligible(self):
        report = END.evaluate_endurance(_series(1), profile="engine-smoke",
                                        duration_min=1.0)
        self.assertFalse(report["eligible_for_release"])


if __name__ == "__main__":
    unittest.main()
