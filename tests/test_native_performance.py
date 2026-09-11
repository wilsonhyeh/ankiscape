# tests/test_native_performance.py - Native measurement contract tests.
"""Verifies measurement boundaries, floors and control pairing for the
native performance orchestrator without launching Anki. The orchestrator's
_evaluate() is the unit under test; the native smoke itself is a CI/local
gate, not a unit test.
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


NP = _load("ankiscape_native_performance_under_test",
           "dev/native_performance.py")


def _addon_raw(**overrides):
    raw = {
        "mode": "perf", "run_mode": "addon", "answers": 20,
        "accept_ms": [120.0, 130.0, 125.0, 140.0, 150.0],
        "reward_ms": [20.0, 25.0, 30.0],
        "lag_ms": [float(i) for i in range(1, 26)],
        "shell_ms": [500.0, 40.0, 45.0, 50.0],
        "cold_ms": 500.0,
        "rebuilds_ms": [3000.0, 3200.0, 2800.0],
        "idle_seconds": 15, "idle_cpu_pct": 0.4,
    }
    raw.update(overrides)
    return raw


def _control_raw(**overrides):
    raw = {
        "mode": "perf", "run_mode": "control", "answers": 20,
        "accept_ms": [], "reward_ms": [],
        "lag_ms": [float(i) / 2 for i in range(1, 26)],
        "shell_ms": [], "cold_ms": None, "rebuilds_ms": [],
        "idle_seconds": 15, "idle_cpu_pct": 0.2,
    }
    raw.update(overrides)
    return raw


def _run(install, raw):
    return {"install_addon": install, "rc": 0, "runtime": {},
            "raw": raw, "journey": "native-performance"}


class NativePerformanceEvaluationTests(unittest.TestCase):
    def test_valid_paired_smoke_passes(self):
        cfg = dict(NP.PROFILES["smoke"])
        runs = [_run(False, _control_raw()), _run(True, _addon_raw())]
        metrics, failures = NP._evaluate("smoke", cfg, runs)
        self.assertEqual(failures, [])
        self.assertIsNotNone(metrics["event_loop_lag"]["p95_ms"])
        self.assertIsNotNone(metrics["reward_completion"]["p95_ms"])
        self.assertIsNotNone(metrics["late_retraction_rebuild"]["max_ms"])
        self.assertEqual(metrics["idle_cpu"]["cpu_pct_delta"], 0.2)

    def test_missing_control_pair_fails(self):
        cfg = dict(NP.PROFILES["smoke"])
        runs = [_run(True, _addon_raw())]
        _metrics, failures = NP._evaluate("smoke", cfg, runs)
        self.assertIn("missing_control_runs", failures)

    def test_floor_counts_are_enforced(self):
        cfg = dict(NP.PROFILES["nightly"])
        runs = [_run(False, _control_raw()), _run(True, _addon_raw())]
        _metrics, failures = NP._evaluate("nightly", cfg, runs)
        self.assertTrue(any(f.startswith("floor:lag_samples") for f in failures),
                        failures)
        self.assertTrue(any(f.startswith("floor:warm_shell") for f in failures),
                        failures)
        self.assertTrue(any(f.startswith("floor:rewards") for f in failures),
                        failures)

    def test_idle_delta_budget_enforced(self):
        cfg = dict(NP.PROFILES["smoke"])
        runs = [_run(False, _control_raw(idle_cpu_pct=0.0)),
                _run(True, _addon_raw(idle_cpu_pct=5.0))]
        _metrics, failures = NP._evaluate("smoke", cfg, runs)
        self.assertTrue(any("idle_cpu" in f for f in failures), failures)

    def test_high_lag_budget_enforced(self):
        cfg = dict(NP.PROFILES["smoke"])
        runs = [_run(False, _control_raw()),
                _run(True, _addon_raw(lag_ms=[900.0] * 10))]
        _metrics, failures = NP._evaluate("smoke", cfg, runs)
        self.assertTrue(any("event_loop_lag" in f for f in failures), failures)

    def test_missing_native_metric_is_failure_never_zero(self):
        cfg = dict(NP.PROFILES["smoke"])
        runs = [_run(False, _control_raw()), _run(True, _addon_raw(lag_ms=[],
                                                                   reward_ms=[]))]
        metrics, failures = NP._evaluate("smoke", cfg, runs)
        self.assertIsNone(metrics["event_loop_lag"]["p95_ms"])
        self.assertTrue(any("event_loop_lag" in f for f in failures), failures)

    def test_shell_open_excludes_cold_first_open(self):
        cfg = dict(NP.PROFILES["smoke"])
        runs = [_run(False, _control_raw()),
                _run(True, _addon_raw(shell_ms=[900.0, 10.0, 20.0],
                                      cold_ms=900.0))]
        metrics, _failures = NP._evaluate("smoke", cfg, runs)
        self.assertLessEqual(metrics["warm_shell_open"]["p95_ms"], 20.0)
        self.assertEqual(metrics["cold_shell_appearance"]["ms"], 900.0)

    def test_profiles_mark_smoke_ineligible_and_release_strict(self):
        self.assertFalse(NP.PROFILES["smoke"]["eligible_for_release"])
        self.assertFalse(NP.PROFILES["nightly"]["eligible_for_release"])
        self.assertTrue(NP.PROFILES["release"]["eligible_for_release"])
        self.assertEqual(NP.PROFILES["smoke"]["repetitions"], 1)
        self.assertEqual(NP.PROFILES["release"]["repetitions"], 3)


def _endurance_report(**overrides):
    report = {"profile": "nightly", "duration_min": 30.0, "sample_count": 60,
              "window_samples": 60, "slope_mib_per_min": 0.2,
              "settled_increase_mib": 4.0, "baseline_rss_mib": 200.0,
              "sample_cadence_s": 30.0, "eligible_for_release": False,
              "pass": True, "failures": []}
    report.update(overrides)
    return report


class NativeEnduranceEvaluationTests(unittest.TestCase):
    """Endurance is its own experiment: no paired control, no timing
    budgets. Its absence or invalidity must still fail."""

    def test_valid_endurance_does_not_require_paired_runs(self):
        cfg = dict(NP.PROFILES["nightly"])
        runs = [{"install_addon": True, "rc": 0, "runtime": {},
                 "journey": "native-performance", "raw": {"mode": "endurance"}}]
        metrics, failures = NP._evaluate("nightly", cfg, runs,
                                         endurance_report=_endurance_report())
        self.assertEqual(failures, [])
        self.assertNotIn("missing_control_runs", failures)
        self.assertFalse(any(f.startswith("budget:") for f in failures))
        self.assertFalse(any(f.startswith("floor:") for f in failures))
        self.assertEqual(metrics["endurance_memory"]["pass"], True)

    def test_failed_endurance_report_fails_evaluation(self):
        cfg = dict(NP.PROFILES["nightly"])
        runs = [{"install_addon": True, "rc": 0, "runtime": {},
                 "journey": "native-performance", "raw": {"mode": "endurance"}}]
        report = _endurance_report()
        report["pass"] = False
        report["failures"] = ["insufficient_samples:1"]
        _metrics, failures = NP._evaluate("nightly", cfg, runs,
                                          endurance_report=report)
        self.assertIn("endurance:insufficient_samples:1", failures)
        self.assertIn("endurance:not_pass", failures)
        self.assertNotIn("missing_control_runs", failures)

    def test_missing_endurance_evidence_still_fails(self):
        cfg = dict(NP.PROFILES["nightly"])
        runs = [{"install_addon": True, "rc": 1, "runtime": {},
                 "journey": "native-performance", "raw": {}}]
        report = {"profile": "nightly", "duration_min": 30.0,
                  "sample_count": 0, "pass": False,
                  "failures": ["endurance_raw_missing"]}
        _metrics, failures = NP._evaluate("nightly", cfg, runs,
                                          endurance_report=report)
        self.assertIn("endurance:endurance_raw_missing", failures)
        self.assertIn("endurance:not_pass", failures)


if __name__ == "__main__":
    unittest.main()
