# tests/test_reliability_evidence.py - Evidence contract falsification tests.
"""Each test falsifies one failure mode of the reliability evidence contract:
empty matrix, absent archive, fabricated paths, stale/copied results,
interrupted runners, omitted scenarios, skipped tests, zero-test success
reports, mixed provenance, budget violations and the percentile estimator.
"""
from __future__ import annotations

import datetime
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
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


REL = _load("ankiscape_reliability_under_test", "dev/reliability.py")
BENCH = _load("ankiscape_perf_bench_under_test", "dev/perf_bench.py")

NOW = datetime.datetime.now(datetime.timezone.utc)


def _iso(offset_minutes: int) -> str:
    return (NOW + datetime.timedelta(minutes=offset_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _sha(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        digest.update(fh.read())
    return digest.hexdigest()


def _write(path: str, payload) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(payload, (dict, list)):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(payload))
    return path


TEST_MATRIX = {
    "schema": "ankiscape-reliability-matrix",
    "version": 1,
    "required_targets": [
        {"os": "macos", "anki": "26.08.1", "qt": "6"},
        {"os": "linux", "anki": "23.10", "qt": "6"},
    ],
    "scenarios": [
        {"id": "python-suite", "stages": ["pr", "nightly", "release"],
         "host": "any"},
        {"id": "backend-local", "stages": ["pr", "nightly", "release"],
         "host": "linux-docker"},
        {"id": "native-matrix", "stages": ["nightly", "release"],
         "host": "targets"},
        {"id": "performance", "stages": ["nightly", "release"],
         "host": "targets"},
        {"id": "hosted-fixtures", "stages": ["nightly", "release"],
         "host": "trusted"},
    ],
    "workflows": {"release": ".github/workflows/release-verify.yml"},
}

METRICS = {
    "accepted_answer_hook": {
        "0": {"samples": 600, "p95_ms": 5.0, "p99_ms": 9.0},
        "1000": {"samples": 600, "p95_ms": 6.0, "p99_ms": 10.0},
        "10000": {"samples": 600, "p95_ms": 7.0, "p99_ms": 11.0},
        "100000": {"samples": 600, "p95_ms": 8.0, "p99_ms": 12.0},
    },
    "reward_completion": {"samples": 600, "p95_ms": 20.0},
    "late_retraction_rebuild": {"max_ms": 4000.0, "samples": 3},
    "warm_review_scaling": {"p95_ratio_100000_over_1000": 1.3},
}


def _scenario(sid, status="pass", assertions=True):
    entry = {"id": sid, "status": status, "exit_status": 0 if status == "pass" else 1,
             "detail": "ok", "command": "cmd"}
    if assertions:
        entry["assertions"] = [{"name": sid, "ok": status == "pass"}]
    return entry


class EvidenceFixture:
    def __init__(self, root: str):
        self.root = root
        self.matrix_path = _write(
            os.path.join(root, "matrix.json"), json.loads(json.dumps(TEST_MATRIX)))
        self.dist = os.path.join(root, "dist")
        os.makedirs(self.dist, exist_ok=True)
        self.archive = _write(
            os.path.join(self.dist, "ankiscape-3.0.0.ankiaddon"), "fake archive")
        _write(os.path.join(self.dist, "manifest.json"), {
            "archive": "ankiscape-3.0.0.ankiaddon",
            "artifact_sha256": _sha(self.archive),
            "version": "3.0.0"})
        self.evidence = os.path.join(root, "evidence")
        self.run_id = "20260910T120000Z"
        self.records = []

    def add_record(self, os_name="macos", anki="26.08.1", qt="6",
                   trusted=True, scenarios=None, counts=None, metrics=None,
                   completed=True, exit_status=0, run_id=None,
                   artifact_sha=None, files=None, started=None, finished=None,
                   version_field=1):
        run_id = run_id or self.run_id
        lane = f"{os_name}-{anki}-qt{qt}"
        lane_dir = os.path.join(self.evidence, run_id, lane)
        os.makedirs(lane_dir, exist_ok=True)
        metrics_path = _write(os.path.join(lane_dir, "metrics.json"),
                              metrics if metrics is not None else METRICS)
        if scenarios is None:
            scenarios = [_scenario("python-suite")]
            if os_name == "linux":
                scenarios.append(_scenario("backend-local"))
            if "native-matrix" in [s["id"] for s in TEST_MATRIX["scenarios"]]:
                scenarios.append(_scenario("native-matrix"))
                scenarios.append(_scenario("performance"))
            if trusted:
                scenarios.append(_scenario("hosted-fixtures"))
        file_entries = files if files is not None else [
            {"path": "metrics.json", "sha256": _sha(metrics_path),
             "bytes": os.path.getsize(metrics_path)}]
        record = {
            "schema": "ankiscape-reliability-record", "version": version_field,
            "kind": "target", "stage": "release", "run_id": run_id,
            "trusted": trusted,
            "target": {"os": os_name, "arch": "arm64", "anki": anki,
                       "anki_actual": anki, "qt": qt, "python": "3.13.2"},
            "source": {"commit": "abc123", "dirty": False},
            "artifact": {"flavor": "ankiaddon",
                         "file": "ankiscape-3.0.0.ankiaddon",
                         "sha256": artifact_sha or _sha(self.archive),
                         "bytes": os.path.getsize(self.archive)},
            "command": "python3 dev/reliability.py run-lane",
            "started_at": started or _iso(-5), "finished_at": finished or _iso(-1),
            "exit_status": exit_status, "completed": completed,
            "scenarios": scenarios,
            "counts": counts or {"tests_passed": 10, "tests_failed": 0,
                                 "tests_skipped": 0},
            "metrics": metrics if metrics is not None else METRICS,
            "files": file_entries,
        }
        path = _write(os.path.join(lane_dir, "record.json"), record)
        self.records.append(path)
        return path, record

    def validate(self, stage="release", matrix_path=None, budgets=None,
                 enforce_samples=True):
        budgets_path = None
        if budgets is not None:
            budgets_path = _write(os.path.join(self.root, "budgets.json"), budgets)
        return REL.validate_evidence(
            matrix_path or self.matrix_path, self.evidence,
            budgets_path=budgets_path, stage=stage, dist_dir=self.dist,
            enforce_samples=enforce_samples)


class ReliabilityEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ankiscape-evidence-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fx = EvidenceFixture(self.tmp)

    # ------------------------------------------------------------ valid case

    def test_valid_evidence_passes(self):
        self.fx.add_record("macos")
        self.fx.add_record("linux", anki="23.10")
        ok, errors, summary = self.fx.validate()
        self.assertTrue(ok, errors)
        self.assertEqual(summary["records"], 2)
        self.assertEqual(summary["targets"]["macos/26.8.1/qt6"], "ok")

    # ------------------------------------------------------------- rejections

    def test_empty_matrix_fails(self):
        self.fx.add_record("macos")
        self.fx.add_record("linux", anki="23.10")
        matrix = _write(os.path.join(self.tmp, "empty.json"),
                        {"required_targets": [], "scenarios": []})
        ok, errors, _ = self.fx.validate(matrix_path=matrix)
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("matrix_empty") for e in errors), errors)

    def test_absent_archive_fails(self):
        self.fx.add_record("macos")
        self.fx.add_record("linux", anki="23.10")
        os.unlink(self.fx.archive)
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("dist_artifact") for e in errors), errors)

    def test_missing_target_fails(self):
        self.fx.add_record("macos")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("missing_target") for e in errors), errors)

    def test_duplicate_target_fails(self):
        self.fx.add_record("macos")
        self.fx.add_record("macos", trusted=False, run_id="20260910T130000Z")
        self.fx.add_record("linux", anki="23.10")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("duplicate_target") for e in errors), errors)

    def test_fabricated_evidence_path_fails(self):
        self.fx.add_record("macos", files=[
            {"path": "does-not-exist.json", "sha256": "0" * 64, "bytes": 1}])
        self.fx.add_record("linux", anki="23.10")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("file_missing") for e in errors), errors)

    def test_file_hash_mismatch_fails(self):
        self.fx.add_record("macos")
        path = os.path.join(os.path.dirname(self.fx.records[0]), "metrics.json")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("tampered")
        self.fx.add_record("linux", anki="23.10")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("file_hash_mismatch") for e in errors), errors)

    def test_file_stale_mtime_fails(self):
        self.fx.add_record("macos")
        path = os.path.join(os.path.dirname(self.fx.records[0]), "metrics.json")
        old = time.time() - 3 * 24 * 3600
        os.utime(path, (old, old))
        self.fx.add_record("linux", anki="23.10")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("file_stale_mtime") for e in errors), errors)

    def test_copied_record_with_foreign_run_id_fails(self):
        path, _record = self.fx.add_record("macos")
        record = json.load(open(path, encoding="utf-8"))
        record["run_id"] = "19990101T000000Z"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        self.fx.add_record("linux", anki="23.10")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("run_id_mismatch") for e in errors), errors)

    def test_interrupted_runner_fails(self):
        self.fx.add_record("macos", completed=False, exit_status=1)
        self.fx.add_record("linux", anki="23.10")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("incomplete") for e in errors), errors)
        self.assertTrue(any(e.startswith("exit_status") for e in errors), errors)

    def test_future_timestamp_fails(self):
        self.fx.add_record("macos", started=_iso(120), finished=_iso(150))
        self.fx.add_record("linux", anki="23.10")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("future_time") for e in errors), errors)

    def test_omitted_scenario_fails(self):
        self.fx.add_record("macos", scenarios=[_scenario("python-suite")])
        self.fx.add_record("linux", anki="23.10")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("missing_scenario") for e in errors), errors)

    def test_skipped_scenario_fails(self):
        scenarios = [_scenario("python-suite"),
                     _scenario("native-matrix", status="skip"),
                     _scenario("performance")]
        self.fx.add_record("macos", scenarios=scenarios)
        self.fx.add_record("linux", anki="23.10")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("skipped_scenario") for e in errors), errors)

    def test_zero_test_success_report_fails(self):
        self.fx.add_record("macos", counts={"tests_passed": 0, "tests_failed": 0,
                                            "tests_skipped": 0})
        self.fx.add_record("linux", anki="23.10",
                           counts={"tests_passed": 0, "tests_failed": 0,
                                   "tests_skipped": 0})
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("zero_tests") for e in errors), errors)

    def test_skipped_tests_fail(self):
        self.fx.add_record("macos", counts={"tests_passed": 5, "tests_failed": 0,
                                            "tests_skipped": 2})
        self.fx.add_record("linux", anki="23.10")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("skipped_tests") for e in errors), errors)

    def test_mixed_artifact_provenance_fails(self):
        self.fx.add_record("macos")
        self.fx.add_record("linux", anki="23.10", artifact_sha="f" * 64)
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("artifact_hash_mismatch") for e in errors),
                        errors)
        self.assertTrue(any(e.startswith("mixed_provenance") for e in errors), errors)

    def test_missing_trusted_scenario_fails(self):
        self.fx.add_record("macos", trusted=False)
        self.fx.add_record("linux", anki="23.10", trusted=False)
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(
            any(e.startswith("missing_trusted_scenario") for e in errors), errors)

    def test_record_version_mismatch_fails(self):
        self.fx.add_record("macos", version_field=99)
        self.fx.add_record("linux", anki="23.10")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("record_version") for e in errors), errors)

    def test_budget_violation_fails_release(self):
        bad = dict(METRICS)
        bad["accepted_answer_hook"] = dict(METRICS["accepted_answer_hook"])
        bad["accepted_answer_hook"]["100000"] = {"samples": 600, "p95_ms": 900.0,
                                                 "p99_ms": 950.0}
        budgets = {
            "budgets": {
                "accepted_answer_hook": {
                    "history_sizes": [0, 1000, 10000, 100000],
                    "metric": "p95_ms", "limit": 20.0,
                    "p99_metric": "p99_ms", "p99_limit": 50.0,
                    "min_samples": 500},
                "warm_review_scaling": {"metric": "p95_ratio_100000_over_1000",
                                        "limit": 2.0},
                "reward_completion": {"metric": "p95_ms", "limit": 250.0,
                                      "min_samples": 500},
                "late_retraction_rebuild": {"metric": "max_ms", "target": 8000.0,
                                            "hard_limit": 10000.0},
            }}
        self.fx.add_record("macos", metrics=bad)
        self.fx.add_record("linux", anki="23.10", metrics=bad)
        ok, errors, _ = self.fx.validate(budgets=budgets)
        self.assertFalse(ok)
        self.assertTrue(any("budget:accepted_answer_hook:p95" in e for e in errors),
                        errors)

    def test_sample_floor_enforced_on_release(self):
        low = dict(METRICS)
        low["accepted_answer_hook"] = {
            "0": {"samples": 10, "p95_ms": 5.0, "p99_ms": 9.0},
            "1000": {"samples": 10, "p95_ms": 5.0, "p99_ms": 9.0},
            "10000": {"samples": 10, "p95_ms": 5.0, "p99_ms": 9.0},
            "100000": {"samples": 10, "p95_ms": 5.0, "p99_ms": 9.0}}
        low["reward_completion"] = {"samples": 10, "p95_ms": 5.0}
        budgets = {
            "budgets": {
                "accepted_answer_hook": {
                    "history_sizes": [0, 1000, 10000, 100000],
                    "metric": "p95_ms", "limit": 20.0,
                    "p99_metric": "p99_ms", "p99_limit": 50.0,
                    "min_samples": 500},
                "reward_completion": {"metric": "p95_ms", "limit": 250.0,
                                      "min_samples": 500},
            }}
        self.fx.add_record("macos", metrics=low)
        self.fx.add_record("linux", anki="23.10", metrics=low)
        ok, errors, _ = self.fx.validate(budgets=budgets, enforce_samples=True)
        self.assertFalse(ok)
        self.assertTrue(any("samples" in e for e in errors), errors)
        ok2, _errors2, _ = self.fx.validate(budgets=budgets, enforce_samples=False)
        # Without the floor, the same numbers pass their latency limits.
        self.assertTrue(ok2)

    def test_no_records_fails(self):
        os.makedirs(self.fx.evidence, exist_ok=True)
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("no_records") for e in errors), errors)


class PercentileTests(unittest.TestCase):
    def test_nearest_rank_is_an_observed_sample(self):
        samples = [5.0, 1.0, 9.0, 3.0]
        self.assertEqual(REL.nearest_rank_percentile(samples, 50), 3.0)
        self.assertEqual(REL.nearest_rank_percentile(samples, 95), 9.0)
        self.assertEqual(REL.nearest_rank_percentile(samples, 100), 9.0)
        self.assertEqual(REL.nearest_rank_percentile([], 95), 0.0)

    def test_p95_never_exceeds_observed_max(self):
        for samples in ([1.0], [1.0, 2.0], [1.0, 2.0, 3.0],
                        [1.0, 2.0, 3.0, 4.0, 5.0]):
            p95 = BENCH._p95(samples)
            self.assertLessEqual(p95, max(samples))
            self.assertIn(p95, samples)

    def test_perf_bench_uses_adequate_samples(self):
        # n=100 nearest-rank p95 is element ceil(0.95*100)=95 of 100
        # (zero-indexed 94), an observed sample.
        samples = [float(i) for i in range(100)]
        self.assertEqual(BENCH._p95(samples), 94.0)


class BudgetEvaluationTests(unittest.TestCase):
    def test_warm_scaling_ratio_enforced(self):
        metrics = {"accepted_answer_hook": {
            "1000": {"samples": 600, "p95_ms": 10.0, "p99_ms": 20.0},
            "100000": {"samples": 600, "p95_ms": 40.0, "p99_ms": 60.0}},
            "warm_review_scaling": {"p95_ratio_100000_over_1000": 4.0},
            "reward_completion": {"samples": 600, "p95_ms": 20.0},
            "late_retraction_rebuild": {"max_ms": 5000.0}}
        budgets = {"budgets": {
            "accepted_answer_hook": {
                "history_sizes": [1000, 100000], "metric": "p95_ms", "limit": 20.0,
                "p99_metric": "p99_ms", "p99_limit": 50.0, "min_samples": 500},
            "warm_review_scaling": {"metric": "p95_ratio_100000_over_1000",
                                    "limit": 2.0},
            "reward_completion": {"metric": "p95_ms", "limit": 250.0,
                                  "min_samples": 500},
            "late_retraction_rebuild": {"metric": "max_ms", "target": 8000.0,
                                        "hard_limit": 10000.0}}}
        failures = REL.evaluate_budgets(metrics, budgets)
        self.assertTrue(any("warm_review_scaling" in f for f in failures), failures)
        self.assertTrue(any("accepted_answer_hook:p95:100000" in f for f in failures),
                        failures)

    def test_late_retraction_hard_gate_and_target(self):
        budgets = {"budgets": {"late_retraction_rebuild": {
            "metric": "max_ms", "target": 8000.0, "hard_limit": 10000.0}}}
        over_hard = REL.evaluate_budgets(
            {"late_retraction_rebuild": {"max_ms": 12000.0}}, budgets)
        self.assertTrue(any(":max:" in f for f in over_hard), over_hard)
        target_miss = REL.evaluate_budgets(
            {"late_retraction_rebuild": {"max_ms": 9000.0}}, budgets)
        self.assertTrue(any("target_missed" in f for f in target_miss), target_miss)
        measured = REL.evaluate_budgets(
            {"late_retraction_rebuild": {"max_ms": 500.0}}, budgets)
        self.assertEqual(measured, [])

    def test_native_budgets_enforced_when_measured(self):
        budgets = {"budgets": {
            "event_loop_lag": {"metric": "p95_ms", "limit": 50.0,
                               "max_limit_ms": 200.0},
            "idle_cpu": {"metric": "cpu_pct_delta", "limit": 1.0},
            "endurance_memory": {"metric": "slope_mib_per_min", "limit": 1.0,
                                 "settled_limit_mib": 50.0}}}
        failures = REL.evaluate_budgets({
            "event_loop_lag": {"p95_ms": 80.0, "max_ms": 300.0},
            "idle_cpu": {"cpu_pct_delta": 5.0, "new_recurring_timer": True},
            "endurance_memory": {"slope_mib_per_min": 3.0,
                                 "settled_increase_mib": 80.0},
        }, budgets)
        self.assertGreaterEqual(len(failures), 4)
        self.assertTrue(any("event_loop_lag" in f for f in failures), failures)
        self.assertTrue(any("idle_cpu" in f for f in failures), failures)
        self.assertTrue(any("endurance_memory" in f for f in failures), failures)

    def test_missing_native_metrics_are_not_failures(self):
        budgets = {"budgets": {"event_loop_lag": {"metric": "p95_ms",
                                                  "limit": 50.0,
                                                  "max_limit_ms": 200.0}}}
        self.assertEqual(REL.evaluate_budgets({}, budgets), [])


class MatrixInventoryTests(unittest.TestCase):
    def test_repo_matrix_has_seven_targets_and_mutation_gate(self):
        matrix = json.load(open(os.path.join(ROOT, "dev",
                                             "reliability-matrix.json"),
                                encoding="utf-8"))
        targets = matrix["required_targets"]
        self.assertEqual(len(targets), 7)
        keys = {REL.target_key(t) for t in targets}
        self.assertIn(("macos", "26.8.1", "6"), keys)
        self.assertIn(("linux", "23.10", "5"), keys)
        ids = {s["id"] for s in matrix["scenarios"]}
        for required in ("python-suite", "native-matrix", "performance",
                         "mutation-gate", "hosted-fixtures"):
            self.assertIn(required, ids)

    def test_version_normalization_matches_zero_padded(self):
        self.assertTrue(REL.version_matches("26.8.1", "26.08.1"))
        self.assertTrue(REL.version_matches("23.10", "23.10.4"))
        self.assertFalse(REL.version_matches("26.8.1", "26.9.0"))

    def test_required_scenarios_are_target_specific(self):
        matrix = json.load(open(os.path.join(ROOT, "dev",
                                             "reliability-matrix.json"),
                                encoding="utf-8"))
        release_linux = set(REL._required_scenarios(matrix, "release", "linux"))
        release_macos = set(REL._required_scenarios(matrix, "release", "macos"))
        self.assertIn("backend-local", release_linux)
        self.assertNotIn("backend-local", release_macos)
        self.assertIn("mutation-gate", release_macos)


class BaselineAndPerfRuntimeTests(unittest.TestCase):
    def test_late_retraction_budget_recorded_as_hard_gate(self):
        budgets = json.load(open(os.path.join(ROOT, "dev",
                                              "reliability-budgets.json"),
                                 encoding="utf-8"))
        entry = budgets["budgets"]["late_retraction_rebuild"]
        self.assertEqual(entry["hard_limit"], 10000.0)
        self.assertEqual(entry["target"], 8000.0)

    def test_budget_history_sizes_cover_zero_to_100k(self):
        budgets = json.load(open(os.path.join(ROOT, "dev",
                                              "reliability-budgets.json"),
                                 encoding="utf-8"))
        self.assertEqual(budgets["budgets"]["accepted_answer_hook"]["history_sizes"],
                         [0, 1000, 10000, 100000])
        self.assertEqual(
            budgets["budgets"]["accepted_answer_hook"]["min_samples"], 500)


if __name__ == "__main__":
    unittest.main()
