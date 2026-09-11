# tests/test_reliability_evidence.py - Evidence contract falsification tests.
"""Each test falsifies one failure mode of the reliability evidence contract:
empty matrix, absent archive, fabricated paths, stale/copied results,
interrupted runners, omitted scenarios, skipped tests, zero-test success
reports, mixed provenance, budget violations, role/job spoofing, false
assertions, missing native runtime identity/metrics/files, wrong candidate
expectations and the percentile estimator.
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
FETCH = _load("ankiscape_fetch_runtimes_under_test", "dev/fetch_runtimes.py")

NOW = datetime.datetime.now(datetime.timezone.utc)
COMMIT = "abc123"
HOSTED_JOURNEYS = ["ui-deferred-rewards", "ui-rebuild-review", "ui-report-bug",
                   "ui-visual-polish", "ui-test-leaderboard",
                   "ui-credential-fallback", "ui-profile-races", "ui-recovery"]


def _iso(offset_minutes: int) -> str:
    return (NOW + datetime.timedelta(minutes=offset_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _sha(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        digest.update(fh.read())
    return digest.hexdigest()


def _read_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


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
    "version": 2,
    "required_targets": [
        {"os": "macos", "anki": "26.08.1", "qt": "6"},
        {"os": "linux", "anki": "23.10", "qt": "6"},
    ],
    "roles": {
        "shared": {"stages": ["nightly", "release"],
                   "jobs": {"nightly": "shared", "release": "shared"},
                   "workflows": {"release": ".github/workflows/release-verify.yml"}},
        "native": {"stages": ["nightly", "release"],
                   "jobs": {"nightly": "native", "release": "lanes"},
                   "workflows": {"release": ".github/workflows/release-verify.yml"}},
        "hosted": {"stages": ["nightly", "release"],
                   "trusted": True,
                   "jobs": {"nightly": "hosted-fixtures", "release": "hosted"},
                   "workflows": {"release": ".github/workflows/release-verify.yml"}},
    },
    "scenarios": [
        {"id": "python-suite", "role": "shared",
         "stages": ["nightly", "release"], "counts": "unittest", "min_tests": 1},
        {"id": "performance", "role": "shared",
         "stages": ["nightly", "release"],
         "require_metrics": ["accepted_answer_hook", "warm_review_scaling",
                             "reward_completion", "late_retraction_rebuild"]},
        {"id": "native-matrix", "role": "native",
         "stages": ["nightly", "release"], "require_files_min": 1},
        {"id": "native-journeys", "role": "native",
         "stages": ["nightly", "release"],
         "require_journeys": ["ui-deferred-rewards", "ui-recovery"],
         "target_requirements": [
             {"target": {"os": "macos", "anki": "26.08.1", "qt": "6"},
              "journey": "ui-test-leaderboard",
              "assertions": ["test_leaderboard_login",
                             "test_leaderboard_public_isolated"]}]},
        {"id": "hosted-fixtures", "role": "hosted",
         "stages": ["nightly", "release"], "require_files_min": 1},
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

BUDGETS = {
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


def _scenario(sid, status="pass", assertions=True, exit_status=0,
              counts=None, files=None, journeys=None, assertion_ok=None):
    entry = {"id": sid, "status": status,
             "exit_status": 0 if status == "pass" and exit_status == 0
             else exit_status or 1,
             "detail": "ok", "command": "cmd", "counts": counts or {},
             "files": files or []}
    if assertions:
        ok = (status == "pass") if assertion_ok is None else bool(assertion_ok)
        entry["assertions"] = [{"name": sid, "ok": ok}]
    if journeys is not None:
        entry["journeys"] = journeys
    return entry


def _journey(name, assertions):
    return {
        "journey": name, "exit_status": 0, "steps": len(assertions),
        "failed": [],
        "assertions": [{"name": n, "ok": ok} for n, ok in assertions],
        "screenshots": [], "files": [],
    }


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

    def _role_scenarios(self, role, *, metrics, journeys, assertions_ok,
                        drop_scenario_files):
        if role == "shared":
            metrics_path = _write(
                os.path.join(self.root, "payload", "performance-metrics.json"),
                {"metrics": metrics})
            scenario_files = [] if drop_scenario_files else [
                {"path": "performance-metrics.json",
                 "sha256": _sha(metrics_path),
                 "bytes": os.path.getsize(metrics_path)}]
            return [
                _scenario("python-suite",
                          counts={"tests_passed": 10, "tests_failed": 0,
                                  "tests_skipped": 0}),
                _scenario("performance", assertion_ok=assertions_ok,
                          files=scenario_files),
            ]
        if role == "native":
            native_file = _write(
                os.path.join(self.root, "payload", "native-matrix-report.md"),
                "report")
            scenario_files = [] if drop_scenario_files else [
                {"path": "native-matrix-report.md",
                 "sha256": _sha(native_file),
                 "bytes": os.path.getsize(native_file)}]
            journeys_list = journeys if journeys is not None else [
                _journey("ui-deferred-rewards", [("deferred", True)]),
                _journey("ui-recovery", [("recovery", True)]),
                _journey("ui-test-leaderboard",
                         [("test_leaderboard_login", True),
                          ("test_leaderboard_public_isolated", True)]),
            ]
            return [
                _scenario("native-matrix", assertion_ok=assertions_ok,
                          files=scenario_files),
                _scenario("native-journeys", journeys=journeys_list),
            ]
        if role == "hosted":
            hosted_file = _write(
                os.path.join(self.root, "payload", "hosted-fixtures-verify.log"),
                "ok")
            scenario_files = [] if drop_scenario_files else [
                {"path": "hosted-fixtures-verify.log",
                 "sha256": _sha(hosted_file),
                 "bytes": os.path.getsize(hosted_file)}]
            return [
                _scenario("hosted-fixtures", files=scenario_files),
            ]
        raise AssertionError(role)

    def add_record(self, role="native", os_name="macos", anki="26.08.1", qt="6",
                   trusted=False, scenarios=None, counts=None, metrics=None,
                   completed=True, exit_status=0, run_id=None,
                   artifact_sha=None, files=None, started=None, finished=None,
                   version_field=2, stage="release", job=None, dirty=False,
                   anki_actual=None, journeys=None, assertions_ok=True,
                   drop_scenario_files=False, lane=None):
        run_id = run_id or self.run_id
        lane = lane or f"{role}-{os_name}-{anki}-qt{qt}"
        lane_dir = os.path.join(self.evidence, run_id, lane)
        os.makedirs(lane_dir, exist_ok=True)
        if metrics is None:
            metrics = dict(METRICS) if role == "shared" else {}
        if scenarios is None:
            scenarios = self._role_scenarios(
                role, metrics=metrics, journeys=journeys,
                assertions_ok=assertions_ok,
                drop_scenario_files=drop_scenario_files)
        # Copy every payload a scenario references into the lane, then fix up
        # its hash/size to the real file.
        for scenario in scenarios:
            for entry in scenario.get("files") or []:
                name = str(entry.get("path"))
                src = os.path.join(self.root, "payload", name)
                dest = os.path.join(lane_dir, name)
                if os.path.isfile(src) and not os.path.isfile(dest):
                    shutil.copyfile(src, dest)
                if os.path.isfile(dest):
                    entry["sha256"] = _sha(dest)
                    entry["bytes"] = os.path.getsize(dest)
        jobs = {"shared": "shared", "native": "lanes",
                "hosted": "hosted"}
        target = {"os": os_name, "arch": "arm64", "python": "3.13.2"}
        if role == "native":
            target.update({"anki": anki,
                           "anki_actual": anki if anki_actual is None
                           else anki_actual,
                           "qt": qt, "qt_actual": f"{qt}.9.0",
                           "anki_python": "3.11.16"})
        if files is None:
            files = []
            for name in sorted(os.listdir(lane_dir)):
                path = os.path.join(lane_dir, name)
                if os.path.isfile(path) and name != "record.json":
                    files.append({"path": name, "sha256": _sha(path),
                                  "bytes": os.path.getsize(path)})
        record = {
            "schema": "ankiscape-reliability-record", "version": version_field,
            "kind": "target", "stage": stage, "role": role,
            "job": job or jobs[role], "run_id": run_id, "trusted": trusted,
            "target": target,
            "source": {"commit": COMMIT, "dirty": dirty},
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
            "metrics": metrics,
            "files": files,
        }
        path = _write(os.path.join(lane_dir, "record.json"), record)
        self.records.append(path)
        return path, record

    def add_valid_set(self, *, macos_metrics=None, macos_journeys=None,
                      macos_assertions_ok=True, macos_dirty=False,
                      macos_anki_actual=None, macos_artifact_sha=None,
                      macos_files=None, run_id=None, macos_scenarios=None,
                      macos_drop_scenario_files=False):
        self.add_record(role="shared", metrics=macos_metrics)
        self.add_record(role="native", os_name="macos", anki="26.08.1",
                        anki_actual=macos_anki_actual, dirty=macos_dirty,
                        journeys=macos_journeys,
                        assertions_ok=macos_assertions_ok,
                        artifact_sha=macos_artifact_sha, files=macos_files,
                        run_id=run_id,
                        drop_scenario_files=macos_drop_scenario_files)
        self.add_record(role="native", os_name="linux", anki="23.10",
                        anki_actual="23.10.4", run_id=run_id)
        self.add_record(role="hosted", trusted=True, job="hosted")

    def validate(self, stage="release", matrix_path=None, budgets=None,
                 enforce_samples=True, expected_commit="",
                 expected_artifact_sha256="", expected_run_id=""):
        budgets_path = None
        if budgets is not None:
            budgets_path = _write(os.path.join(self.root, "budgets.json"), budgets)
        return REL.validate_evidence(
            matrix_path or self.matrix_path, self.evidence,
            budgets_path=budgets_path, stage=stage, dist_dir=self.dist,
            enforce_samples=enforce_samples, expected_commit=expected_commit,
            expected_artifact_sha256=expected_artifact_sha256,
            expected_run_id=expected_run_id)


class ReliabilityEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ankiscape-evidence-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fx = EvidenceFixture(self.tmp)

    # ------------------------------------------------------------ valid case

    def test_valid_evidence_passes(self):
        self.fx.add_valid_set()
        ok, errors, summary = self.fx.validate()
        self.assertTrue(ok, errors)
        self.assertEqual(summary["records"], 4)
        self.assertEqual(summary["targets"]["macos/26.8.1/qt6"], "ok")

    def test_valid_evidence_passes_with_expectations(self):
        self.fx.add_valid_set()
        ok, errors, _ = self.fx.validate(
            expected_commit=COMMIT,
            expected_artifact_sha256=_sha(self.fx.archive),
            expected_run_id=self.fx.run_id)
        self.assertTrue(ok, errors)

    # ------------------------------------------------------------- rejections

    def test_wrong_stage_fails(self):
        self.fx.add_valid_set()
        # Rebuild both native records under the nightly stage.
        self.fx.evidence = os.path.join(self.tmp, "nightly")
        os.makedirs(self.fx.evidence, exist_ok=True)
        self.fx.records = []
        self.fx.add_valid_set()
        for path in self.fx.records:
            record = _read_json(path)
            if record["role"] in ("shared", "native"):
                os.unlink(path)
        ok, errors, _ = self.fx.validate(stage="nightly")
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("missing_role") for e in errors), errors)

    def test_stage_mismatch_fails(self):
        self.fx.add_valid_set()
        path = self.fx.records[0]
        record = _read_json(path)
        record["stage"] = "pr"
        _write(path, record)
        ok, errors, _ = self.fx.validate(stage="release")
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("stage_mismatch") for e in errors), errors)

    def test_dirty_source_fails(self):
        self.fx.add_valid_set(macos_dirty=True)
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("dirty_source") for e in errors), errors)

    def test_wrong_expected_commit_fails(self):
        self.fx.add_valid_set()
        ok, errors, _ = self.fx.validate(expected_commit="f" * 40)
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("commit_expected") for e in errors), errors)

    def test_wrong_expected_artifact_fails(self):
        self.fx.add_valid_set()
        ok, errors, _ = self.fx.validate(expected_artifact_sha256="f" * 64)
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("artifact_expected") for e in errors),
                        errors)

    def test_wrong_expected_run_id_fails(self):
        self.fx.add_valid_set()
        ok, errors, _ = self.fx.validate(expected_run_id="19990101T000000Z")
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("run_missing") for e in errors), errors)

    def test_false_actual_runtime_fails(self):
        self.fx.add_valid_set(macos_anki_actual="23.10")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("actual_runtime_mismatch") for e in errors),
                        errors)

    def test_missing_observed_runtime_fails(self):
        self.fx.add_valid_set(macos_anki_actual="")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(
            any(e.startswith("missing_observed_runtime") for e in errors), errors)

    def test_false_assertion_fails(self):
        self.fx.add_valid_set(macos_assertions_ok=False)
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("assertion_failed") for e in errors),
                        errors)

    def test_scenario_exit_status_fails(self):
        path, record = self.fx.add_record(role="shared")
        record["scenarios"][0]["exit_status"] = 1
        _write(path, record)
        self.fx.add_record(role="native", os_name="macos")
        self.fx.add_record(role="native", os_name="linux", anki="23.10")
        self.fx.add_record(role="hosted", trusted=True, job="hosted")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("scenario_exit") for e in errors), errors)

    def test_job_spoof_fails(self):
        self.fx.add_valid_set()
        path = self.fx.records[-1]
        record = _read_json(path)
        record["job"] = "not-a-real-job"
        _write(path, record)
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("job_mismatch") for e in errors), errors)

    def test_untrusted_hosted_role_fails(self):
        self.fx.add_valid_set()
        path = self.fx.records[-1]
        record = _read_json(path)
        record["trusted"] = False
        _write(path, record)
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("untrusted_role") for e in errors), errors)

    def test_role_target_impersonation_fails(self):
        self.fx.add_valid_set()
        path = self.fx.records[0]
        record = _read_json(path)
        record["target"]["anki"] = "26.08.1"
        record["target"]["qt"] = "6"
        _write(path, record)
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("role_target_impersonation") for e in errors),
                        errors)

    def test_duplicate_role_fails(self):
        self.fx.add_valid_set()
        self.fx.add_record(role="shared", lane="shared-extra")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("duplicate_role") for e in errors), errors)

    def test_missing_native_metrics_fails(self):
        self.fx.add_valid_set(macos_metrics={})
        # Shared role owns the metrics requirement; give the shared record an
        # empty metrics object with matching budgets.
        self.fx.add_valid_set()
        path = self.fx.records[0]
        record = _read_json(path)
        record["metrics"] = {}
        _write(path, record)
        ok, errors, _ = self.fx.validate(budgets=BUDGETS)
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("missing_metric") for e in errors), errors)

    def test_nan_metric_fails(self):
        self.fx.add_valid_set()
        path = self.fx.records[0]
        record = _read_json(path)
        record["metrics"]["reward_completion"]["p95_ms"] = float("nan")
        _write(path, record)
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("metric_invalid") for e in errors), errors)

    def test_contradictory_metric_summary_fails(self):
        self.fx.add_valid_set()
        path = self.fx.records[0]
        record = _read_json(path)
        record["metrics"]["accepted_answer_hook"]["100000"]["p50_ms"] = 99.0
        _write(path, record)
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any("order" in e for e in errors), errors)

    def test_missing_scenario_files_fails(self):
        self.fx.add_valid_set(macos_drop_scenario_files=True)
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("scenario_files") for e in errors), errors)

    def test_unsafe_scenario_file_path_fails(self):
        path, record = self.fx.add_record(role="native", os_name="macos")
        record["scenarios"][0]["files"] = [
            {"path": "../escape.txt", "sha256": "0" * 64, "bytes": 1}]
        _write(path, record)
        self.fx.add_record(role="native", os_name="linux", anki="23.10")
        self.fx.add_record(role="shared")
        self.fx.add_record(role="hosted", trusted=True, job="hosted")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("unsafe_file_path") for e in errors), errors)

    def test_missing_hosted_native_assertion_fails(self):
        journeys = [
            _journey("ui-deferred-rewards", [("deferred", True)]),
            _journey("ui-recovery", [("recovery", True)]),
            # Public-only test with credentials missing: no login assertion.
            _journey("ui-test-leaderboard",
                     [("test_leaderboard_credentials", True),
                      ("test_leaderboard_public_isolated", True)]),
        ]
        self.fx.add_valid_set(macos_journeys=journeys)
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(
            any(e.startswith("missing_hosted_assertion") for e in errors), errors)

    def test_missing_journey_fails(self):
        self.fx.add_valid_set(macos_journeys=[
            _journey("ui-deferred-rewards", [("deferred", True)])])
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("missing_journey") for e in errors), errors)

    def test_empty_matrix_fails(self):
        self.fx.add_valid_set()
        matrix = _write(os.path.join(self.tmp, "empty.json"),
                        {"version": 2, "roles": TEST_MATRIX["roles"],
                         "required_targets": [], "scenarios": []})
        ok, errors, _ = self.fx.validate(matrix_path=matrix)
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("matrix_empty") for e in errors), errors)

    def test_outdated_matrix_fails(self):
        self.fx.add_valid_set()
        matrix = _write(os.path.join(self.tmp, "old.json"),
                        {"version": 1, "required_targets": [{"os": "macos"}],
                         "scenarios": []})
        ok, errors, _ = self.fx.validate(matrix_path=matrix)
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("matrix_outdated") for e in errors), errors)

    def test_absent_archive_fails(self):
        self.fx.add_valid_set()
        os.unlink(self.fx.archive)
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("dist_artifact") for e in errors), errors)

    def test_missing_target_fails(self):
        self.fx.add_record(role="native", os_name="macos")
        self.fx.add_record(role="shared")
        self.fx.add_record(role="hosted", trusted=True, job="hosted")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("missing_target") for e in errors), errors)

    def test_duplicate_target_fails(self):
        self.fx.add_valid_set()
        self.fx.add_record(role="native", os_name="macos",
                           lane="native-macos-duplicate")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("duplicate_targets") for e in errors), errors)

    def test_fabricated_evidence_path_fails(self):
        self.fx.add_valid_set(macos_files=[
            {"path": "does-not-exist.json", "sha256": "0" * 64, "bytes": 1}])
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("file_missing") for e in errors), errors)

    def test_file_hash_mismatch_fails(self):
        self.fx.add_valid_set()
        path = os.path.join(os.path.dirname(self.fx.records[1]),
                            "native-matrix-report.md")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("tampered")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("file_hash_mismatch") for e in errors), errors)

    def test_file_stale_mtime_fails(self):
        self.fx.add_valid_set()
        path = os.path.join(os.path.dirname(self.fx.records[1]),
                            "native-matrix-report.md")
        old = time.time() - 3 * 24 * 3600
        os.utime(path, (old, old))
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("file_stale_mtime") for e in errors), errors)

    def test_copied_record_with_foreign_run_id_fails(self):
        self.fx.add_valid_set()
        path = self.fx.records[0]
        record = _read_json(path)
        record["run_id"] = "19990101T000000Z"
        _write(path, record)
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("run_id_mismatch") for e in errors), errors)

    def test_interrupted_runner_fails(self):
        path, record = self.fx.add_record(role="shared")
        record["completed"] = False
        record["exit_status"] = 1
        _write(path, record)
        self.fx.add_record(role="native", os_name="macos")
        self.fx.add_record(role="native", os_name="linux", anki="23.10")
        self.fx.add_record(role="hosted", trusted=True, job="hosted")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("incomplete") for e in errors), errors)
        self.assertTrue(any(e.startswith("exit_status") for e in errors), errors)

    def test_future_timestamp_fails(self):
        self.fx.add_valid_set()
        path = self.fx.records[1]
        record = _read_json(path)
        record["started_at"] = _iso(120)
        record["finished_at"] = _iso(150)
        _write(path, record)
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("future_time") for e in errors), errors)

    def test_omitted_scenario_fails(self):
        path, record = self.fx.add_record(role="shared")
        record["scenarios"] = [_scenario("python-suite")]
        _write(path, record)
        self.fx.add_record(role="native", os_name="macos")
        self.fx.add_record(role="native", os_name="linux", anki="23.10")
        self.fx.add_record(role="hosted", trusted=True, job="hosted")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("missing_scenario") for e in errors), errors)

    def test_skipped_scenario_fails(self):
        path, record = self.fx.add_record(role="native", os_name="macos")
        for item in record["scenarios"]:
            if item["id"] == "native-matrix":
                item["status"] = "skip"
                item["exit_status"] = 1
        _write(path, record)
        self.fx.add_record(role="native", os_name="linux", anki="23.10")
        self.fx.add_record(role="shared")
        self.fx.add_record(role="hosted", trusted=True, job="hosted")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("scenario_status") for e in errors), errors)

    def test_zero_test_success_report_fails(self):
        path, record = self.fx.add_record(role="shared")
        for item in record["scenarios"]:
            if item["id"] == "python-suite":
                item["counts"] = {"tests_passed": 0, "tests_failed": 0,
                                  "tests_skipped": 0}
        _write(path, record)
        self.fx.add_record(role="native", os_name="macos")
        self.fx.add_record(role="native", os_name="linux", anki="23.10")
        self.fx.add_record(role="hosted", trusted=True, job="hosted")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("too_few_tests") for e in errors), errors)

    def test_skipped_tests_fail(self):
        path, record = self.fx.add_record(role="shared")
        for item in record["scenarios"]:
            if item["id"] == "python-suite":
                item["counts"] = {"tests_passed": 5, "tests_failed": 0,
                                  "tests_skipped": 2}
        _write(path, record)
        self.fx.add_record(role="native", os_name="macos")
        self.fx.add_record(role="native", os_name="linux", anki="23.10")
        self.fx.add_record(role="hosted", trusted=True, job="hosted")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("test_skips") for e in errors), errors)

    def test_mixed_artifact_provenance_fails(self):
        self.fx.add_valid_set(macos_artifact_sha="f" * 64)
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("artifact_hash_mismatch") for e in errors),
                        errors)
        self.assertTrue(any(e.startswith("mixed_provenance") for e in errors), errors)

    def test_missing_hosted_role_fails(self):
        self.fx.add_record(role="shared")
        self.fx.add_record(role="native", os_name="macos")
        self.fx.add_record(role="native", os_name="linux", anki="23.10")
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("missing_role") for e in errors), errors)

    def test_record_version_mismatch_fails(self):
        self.fx.add_valid_set()
        path = self.fx.records[0]
        record = _read_json(path)
        record["version"] = 99
        _write(path, record)
        ok, errors, _ = self.fx.validate()
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith("record_version") for e in errors), errors)

    def test_budget_violation_fails_release(self):
        bad = dict(METRICS)
        bad["accepted_answer_hook"] = dict(METRICS["accepted_answer_hook"])
        bad["accepted_answer_hook"]["100000"] = {"samples": 600, "p95_ms": 900.0,
                                                 "p99_ms": 950.0}
        self.fx.add_valid_set(macos_metrics=bad)
        ok, errors, _ = self.fx.validate(budgets=BUDGETS)
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
        self.fx.add_valid_set(macos_metrics=low)
        ok, errors, _ = self.fx.validate(budgets=BUDGETS, enforce_samples=True)
        self.assertFalse(ok)
        self.assertTrue(any("samples" in e for e in errors), errors)
        ok2, errors2, _ = self.fx.validate(budgets=BUDGETS, enforce_samples=False)
        self.assertTrue(ok2, errors2)

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
        failures = REL.evaluate_budgets(metrics, BUDGETS)
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
        }, budgets, required={"event_loop_lag", "idle_cpu", "endurance_memory"})
        self.assertGreaterEqual(len(failures), 4)
        self.assertTrue(any("event_loop_lag" in f for f in failures), failures)
        self.assertTrue(any("idle_cpu" in f for f in failures), failures)
        self.assertTrue(any("endurance_memory" in f for f in failures), failures)

    def test_required_native_metrics_fail_when_missing(self):
        budgets = {"budgets": {
            "event_loop_lag": {"metric": "p95_ms", "limit": 50.0,
                               "max_limit_ms": 200.0}}}
        failures = REL.evaluate_budgets({}, budgets, required={"event_loop_lag"})
        self.assertTrue(any("not_measured" in f for f in failures), failures)

    def test_missing_native_metrics_are_not_failures(self):
        budgets = {"budgets": {"event_loop_lag": {"metric": "p95_ms",
                                                  "limit": 50.0,
                                                  "max_limit_ms": 200.0}}}
        self.assertEqual(REL.evaluate_budgets({}, budgets), [])


class MatrixInventoryTests(unittest.TestCase):
    def test_repo_matrix_has_seven_targets_and_roles(self):
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
                         "mutation-gate", "hosted-fixtures", "native-performance",
                         "endurance-2h", "account-contracts", "sync-local"):
            self.assertIn(required, ids)
        for scenario in matrix["scenarios"]:
            self.assertIn(scenario.get("role"), matrix["roles"],
                          scenario.get("id"))

    def test_required_scenarios_are_native_only(self):
        matrix = json.load(open(os.path.join(ROOT, "dev",
                                             "reliability-matrix.json"),
                                encoding="utf-8"))
        release = set(REL._required_scenarios(matrix, "release", "linux"))
        self.assertIn("native-matrix", release)
        self.assertIn("native-performance", release)
        self.assertNotIn("backend-local", release)
        self.assertNotIn("mutation-gate", release)
        self.assertNotIn("hosted-fixtures", release)

    def test_version_normalization_matches_zero_padded(self):
        self.assertTrue(REL.version_matches("26.8.1", "26.08.1"))
        self.assertTrue(REL.version_matches("23.10", "23.10.4"))
        self.assertFalse(REL.version_matches("26.8.1", "26.9.0"))


class RuntimeSelectionTests(unittest.TestCase):
    def test_linux_23_10_qt6_selects_qt6_entry(self):
        manifest = FETCH.load_manifest()
        entry = FETCH.find_target(manifest, "linux/23.10", qt="6",
                                  arch="x86_64")
        self.assertEqual(entry["qt"], "6")
        self.assertIn("qt6", entry["url"])

    def test_linux_23_10_qt5_selects_qt5_entry(self):
        manifest = FETCH.load_manifest()
        entry = FETCH.find_target(manifest, "linux/23.10", qt="5",
                                  arch="x86_64")
        self.assertEqual(entry["qt"], "5")
        self.assertIn("qt5", entry["url"])

    def test_ambiguous_target_rejected(self):
        manifest = FETCH.load_manifest()
        with self.assertRaises(SystemExit):
            FETCH.find_target(manifest, "linux/23.10")

    def test_wrong_qt_rejected(self):
        manifest = FETCH.load_manifest()
        with self.assertRaises(SystemExit):
            FETCH.find_target(manifest, "macos/26.8.1", qt="5")

    def test_runtime_dir_is_qt_arch_qualified(self):
        manifest = FETCH.load_manifest()
        entry = FETCH.find_target(manifest, "linux/23.10", qt="5",
                                  arch="x86_64")
        path = FETCH.runtime_dir("/tmp/runtimes", entry)
        self.assertTrue(path.endswith("linux/23.10/qt5-x86_64"), path)


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
