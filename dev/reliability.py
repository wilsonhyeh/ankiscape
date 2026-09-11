#!/usr/bin/env python3
# dev/reliability.py - Reliability orchestrator and evidence validator.
# Dev-only: never shipped inside the add-on package.
"""One command surface for the plan's reliability gates.

  python3 dev/reliability.py verify --stage pr
  python3 dev/reliability.py verify --stage nightly
  python3 dev/reliability.py verify --stage release --evidence artifacts/reliability
  python3 dev/reliability.py verify-evidence --matrix dev/reliability-matrix.json \
      --evidence artifacts/reliability
  python3 dev/reliability.py baseline
  python3 dev/reliability.py run-lane --stage release --out artifacts/reliability/<run>/<lane>

`verify --stage` runs every scenario executable on this machine and accepts
target-specific scenarios only from validated evidence records. A local run
with missing native/hosted coverage fails and names the CI workflow that
provides it; it never pretends to run Windows on a Mac. `verify-evidence`
validates provenance, freshness, artifact hash, scenario completeness, file
hashes and budgets without executing anything.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MATRIX = os.path.join(ROOT, "dev", "reliability-matrix.json")
DEFAULT_BUDGETS = os.path.join(ROOT, "dev", "reliability-budgets.json")
DEFAULT_EVIDENCE = os.path.join(ROOT, "artifacts", "reliability")
DIST_DIR = os.path.join(ROOT, "dist")

RECORD_SCHEMA = "ankiscape-reliability-record"
RECORD_VERSION = 1
STAGES = ("pr", "nightly", "release")
FILE_MTIME_SLACK_S = 600
MAX_RUN_SECONDS = 48 * 3600

# Native journeys introduced by the coverage task; run on each target lane.
NEW_JOURNEYS = ("ui-deferred-rewards", "ui-rebuild-review", "ui-report-bug",
                "ui-visual-polish", "ui-test-leaderboard", "ui-credential-fallback",
                "ui-profile-races", "ui-recovery")


class ReliabilityError(Exception):
    pass


# ---------------------------------------------------------------- primitives

def nearest_rank_percentile(values: List[float], percentile: float) -> float:
    """Nearest-rank percentile, documented explicitly.

    For sorted samples x[1..n], the p-th percentile is x[ceil(p/100 * n)].
    No interpolation is performed, so the returned value is always an
    observed sample and can never exceed max(x). Empty input returns 0.0.
    """
    if not values:
        return 0.0
    xs = sorted(float(v) for v in values)
    rank = max(1, math.ceil((percentile / 100.0) * len(xs)))
    return xs[min(rank, len(xs)) - 1]


def percentile_summary(values: List[float]) -> Dict[str, float]:
    return {
        "samples": len(values),
        "p50_ms": round(nearest_rank_percentile(values, 50), 3),
        "p95_ms": round(nearest_rank_percentile(values, 95), 3),
        "p99_ms": round(nearest_rank_percentile(values, 99), 3),
        "max_ms": round(max(values), 3) if values else 0.0,
        "min_ms": round(min(values), 3) if values else 0.0,
    }


def normalize_version(value: Any) -> str:
    """Zero-pad-insensitive version normalization: 26.08.1 -> 26.8.1."""
    parts = []
    for chunk in str(value or "").strip().split("."):
        try:
            parts.append(str(int(chunk)))
        except ValueError:
            parts.append(chunk)
    return ".".join(parts)


def target_key(target: Dict[str, Any]) -> Tuple[str, str, str]:
    return (str(target.get("os", "")).lower(), normalize_version(target.get("anki")),
            normalize_version(target.get("qt")))


def version_matches(requested: Any, actual: Any) -> bool:
    want = normalize_version(requested).split(".")
    got = normalize_version(actual).split(".")
    return len(got) >= len(want) and got[:len(want)] == want


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: Any) -> Optional[datetime.datetime]:
    try:
        text = str(value).replace("Z", "+00:00")
        stamp = datetime.datetime.fromisoformat(text)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        return stamp.astimezone(datetime.timezone.utc)
    except (ValueError, TypeError):
        return None


def load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def git_source() -> Dict[str, Any]:
    commit = ""
    dirty = None
    try:
        commit = subprocess.run(
            ["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True,
            text=True, timeout=30).stdout.strip()
        status = subprocess.run(
            ["git", "-C", ROOT, "status", "--porcelain"], capture_output=True,
            text=True, timeout=30).stdout.strip()
        dirty = bool(status)
    except OSError:
        pass
    return {"commit": commit, "dirty": dirty}


def system_info() -> Dict[str, Any]:
    info = {"os": platform.system().lower(), "arch": platform.machine(),
            "cpu": "", "ram_gb": None, "os_release": platform.release(),
            "python": platform.python_version()}
    try:
        if sys.platform == "darwin":
            info["cpu"] = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True,
                text=True, timeout=10).stdout.strip()
            mem = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                 text=True, timeout=10).stdout.strip()
            if mem:
                info["ram_gb"] = round(int(mem) / (1024 ** 3), 1)
        elif sys.platform.startswith("linux"):
            with open("/proc/cpuinfo", encoding="utf-8") as fh:
                for line in fh:
                    if line.lower().startswith("model name"):
                        info["cpu"] = line.split(":", 1)[1].strip()
                        break
            with open("/proc/meminfo", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("MemTotal"):
                        info["ram_gb"] = round(
                            int(line.split()[1]) / (1024 ** 2), 1)
                        break
        elif sys.platform.startswith("win"):
            info["cpu"] = os.environ.get("PROCESSOR_IDENTIFIER", "")
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return info


def resolve_artifact(dist_dir: str = DIST_DIR) -> Tuple[Dict[str, Any], str, str]:
    """One expected artifact from dist/manifest.json - never a first glob."""
    manifest_path = os.path.join(dist_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        raise ReliabilityError(f"dist manifest missing: {manifest_path}")
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ReliabilityError("dist manifest is not an object")
    archive = str(manifest.get("archive") or "")
    if not archive:
        raise ReliabilityError("dist manifest has no archive name")
    archive_path = os.path.join(dist_dir, archive)
    if not os.path.isfile(archive_path):
        raise ReliabilityError(f"manifest artifact missing: {archive_path}")
    digest = sha256_file(archive_path)
    expected = str(manifest.get("artifact_sha256") or "")
    if not expected:
        raise ReliabilityError("dist manifest has no artifact_sha256")
    if digest != expected:
        raise ReliabilityError(
            f"artifact hash mismatch: {digest[:12]} != manifest {expected[:12]}")
    return manifest, archive_path, digest


# ------------------------------------------------------------------- budgets

def evaluate_budgets(metrics: Dict[str, Any], budgets: Dict[str, Any], *,
                     enforce_samples: bool = True,
                     required: Optional[set] = None) -> List[str]:
    """Compare perf_runtime metrics against dev/reliability-budgets.json.

    Returns a list of failure strings (empty == pass). Missing native
    metrics are only failures when their budget id is listed in `required`.
    """
    failures: List[str] = []
    cfg = (budgets or {}).get("budgets", {}) or {}
    metrics = metrics or {}
    required = required if required is not None else {
        "accepted_answer_hook", "warm_review_scaling", "reward_completion",
        "late_retraction_rebuild"}

    def _fail(budget_id: str, detail: str) -> None:
        failures.append(f"budget:{budget_id}:{detail}")

    # accepted_answer_hook: p95/p99 per history size with sample floor.
    if "accepted_answer_hook" in cfg:
        hook = cfg["accepted_answer_hook"]
        measured = metrics.get("accepted_answer_hook") or {}
        for size in hook.get("history_sizes", []):
            entry = measured.get(str(size))
            if not entry:
                if "accepted_answer_hook" in required:
                    _fail("accepted_answer_hook", f"not_measured:{size}")
                continue
            samples = int(entry.get("samples", 0) or 0)
            if enforce_samples and samples < int(hook.get("min_samples", 0)):
                _fail("accepted_answer_hook", f"samples:{size}:{samples}")
            if float(entry.get("p95_ms", 1e9)) > float(hook["limit"]):
                _fail("accepted_answer_hook", f"p95:{size}:{entry.get('p95_ms')}")
            if float(entry.get("p99_ms", 1e9)) > float(hook["p99_limit"]):
                _fail("accepted_answer_hook", f"p99:{size}:{entry.get('p99_ms')}")

    # warm_review_scaling: derived p95 ratio 100k / 1k.
    if "warm_review_scaling" in cfg and "warm_review_scaling" in required:
        hook = metrics.get("accepted_answer_hook") or {}
        small = (hook.get("1000") or {}).get("p95_ms")
        large = (hook.get("100000") or {}).get("p95_ms")
        ratio = metrics.get("warm_review_scaling", {}).get(
            "p95_ratio_100000_over_1000")
        if ratio is None and small and large:
            ratio = float(large) / float(small)
        if ratio is None:
            _fail("warm_review_scaling", "not_measured")
        elif float(ratio) > float(cfg["warm_review_scaling"]["limit"]):
            _fail("warm_review_scaling", f"ratio:{round(float(ratio), 3)}")

    # reward_completion
    if "reward_completion" in cfg:
        entry = metrics.get("reward_completion") or {}
        if not entry:
            if "reward_completion" in required:
                _fail("reward_completion", "not_measured")
        else:
            if enforce_samples and int(entry.get("samples", 0)) < int(
                    cfg["reward_completion"].get("min_samples", 0)):
                _fail("reward_completion", f"samples:{entry.get('samples')}")
            if float(entry.get("p95_ms", 1e9)) > float(cfg["reward_completion"]["limit"]):
                _fail("reward_completion", f"p95:{entry.get('p95_ms')}")

    # late_retraction_rebuild: max of runs against hard gate (target reported).
    if "late_retraction_rebuild" in cfg:
        entry = metrics.get("late_retraction_rebuild") or {}
        if not entry:
            if "late_retraction_rebuild" in required:
                _fail("late_retraction_rebuild", "not_measured")
        else:
            worst = float(entry.get("max_ms", 1e9))
            hard = float(cfg["late_retraction_rebuild"]["hard_limit"])
            if worst > hard:
                _fail("late_retraction_rebuild", f"max:{worst}")
            elif worst > float(cfg["late_retraction_rebuild"]["target"]):
                failures.append(
                    f"budget:late_retraction_rebuild:target_missed:{worst}")

    # Native-only budgets: enforced when measured.
    lag = metrics.get("event_loop_lag")
    if lag:
        if float(lag.get("p95_ms", 1e9)) > float(cfg["event_loop_lag"]["limit"]):
            _fail("event_loop_lag", f"p95:{lag.get('p95_ms')}")
        if float(lag.get("max_ms", 0)) > float(cfg["event_loop_lag"]["max_limit_ms"]):
            _fail("event_loop_lag", f"max:{lag.get('max_ms')}")
    shell = metrics.get("warm_shell_open")
    if shell and float(shell.get("p95_ms", 1e9)) > float(cfg["warm_shell_open"]["limit"]):
        _fail("warm_shell_open", f"p95:{shell.get('p95_ms')}")
    cold = metrics.get("cold_shell_appearance")
    if cold and float(cold.get("ms", 1e9)) > float(cfg["cold_shell_appearance"]["limit"]):
        _fail("cold_shell_appearance", f"ms:{cold.get('ms')}")
    idle = metrics.get("idle_cpu")
    if idle:
        if float(idle.get("cpu_pct_delta", 1e9)) > float(cfg["idle_cpu"]["limit"]):
            _fail("idle_cpu", f"delta:{idle.get('cpu_pct_delta')}")
        if idle.get("network_polling") not in (None, "none", "existing_scheduled_sync"):
            _fail("idle_cpu", f"network_polling:{idle.get('network_polling')}")
        if idle.get("new_recurring_timer"):
            _fail("idle_cpu", "new_recurring_timer")
    mem = metrics.get("endurance_memory")
    if mem:
        if float(mem.get("slope_mib_per_min", 1e9)) > float(
                cfg["endurance_memory"]["limit"]):
            _fail("endurance_memory", f"slope:{mem.get('slope_mib_per_min')}")
        if float(mem.get("settled_increase_mib", 1e9)) > float(
                cfg["endurance_memory"]["settled_limit_mib"]):
            _fail("endurance_memory", f"settled:{mem.get('settled_increase_mib')}")
    return failures


# ----------------------------------------------------------------- validate

def _check(condition: bool, errors: List[str], message: str) -> bool:
    if not condition:
        errors.append(message)
    return condition


def _required_scenarios(matrix: Dict[str, Any], stage: str,
                        target_os: str) -> List[str]:
    required = []
    for scenario in matrix.get("scenarios", []):
        if stage not in (scenario.get("stages") or []):
            continue
        host = str(scenario.get("host", "any"))
        if host in ("any", "targets") or (host == "linux" and target_os == "linux") \
                or (host == "linux-docker" and target_os == "linux") \
                or (host == "linux-native" and target_os == "linux"):
            required.append(str(scenario.get("id")))
    return required


def _collect_records(evidence_dir: str) -> List[Tuple[str, Dict[str, Any]]]:
    found: List[Tuple[str, Dict[str, Any]]] = []
    for base, _dirs, names in os.walk(evidence_dir):
        for name in sorted(names):
            if name != "record.json":
                continue
            path = os.path.join(base, name)
            try:
                record = load_json(path)
            except (OSError, ValueError):
                found.append((path, {}))
                continue
            if isinstance(record, dict) and record.get("kind") == "baseline":
                continue
            found.append((path, record if isinstance(record, dict) else {}))
    return found


def validate_evidence(matrix_path: str, evidence_dir: str, *,
                      budgets_path: Optional[str] = None, stage: str = "release",
                      dist_dir: str = DIST_DIR,
                      enforce_samples: bool = True) -> Tuple[bool, List[str], Dict[str, Any]]:
    """Validate evidence records against the matrix. Pure and unit-testable."""
    errors: List[str] = []
    summary: Dict[str, Any] = {"stage": stage, "targets": {}, "records": 0}

    if not os.path.isfile(matrix_path):
        return False, [f"matrix_missing:{matrix_path}"], summary
    try:
        matrix = load_json(matrix_path)
    except (OSError, ValueError) as exc:
        return False, [f"matrix_unreadable:{exc!r}"], summary
    required_targets = list(matrix.get("required_targets") or [])
    if not required_targets:
        return False, ["matrix_empty:required_targets"], summary

    if not os.path.isdir(evidence_dir):
        return False, [f"evidence_missing:{evidence_dir}"], summary
    records = _collect_records(evidence_dir)
    if not records:
        return False, [f"no_records:{evidence_dir}"], summary
    summary["records"] = len(records)

    budgets = None
    if budgets_path:
        try:
            budgets = load_json(budgets_path)
        except (OSError, ValueError) as exc:
            errors.append(f"budgets_unreadable:{exc!r}")

    try:
        manifest, _archive_path, dist_hash = resolve_artifact(dist_dir)
    except ReliabilityError as exc:
        manifest, dist_hash = None, None
        errors.append(f"dist_artifact:{exc}")

    now = datetime.datetime.now(datetime.timezone.utc)

    # Index target records by normalized key.
    by_target: Dict[Tuple[str, str, str], List[Tuple[str, Dict[str, Any]]]] = {}
    for path, record in records:
        target = record.get("target") or {}
        if not record:
            errors.append(f"record_unreadable:{os.path.relpath(path, ROOT)}")
            continue
        by_target.setdefault(target_key(target), []).append((path, record))

    commits = set()
    hashes = set()
    for required in required_targets:
        key = target_key(required)
        matches = by_target.get(key, [])
        rel = os.path.relpath(matches[0][0], ROOT) if matches else "-"
        summary["targets"][f"{key[0]}/{key[1]}/qt{key[2]}"] = "missing"
        if not matches:
            errors.append(f"missing_target:{key}")
            continue
        if len(matches) > 1:
            errors.append(f"duplicate_target:{key}")
            continue
        path, record = matches[0]
        rel = os.path.relpath(path, ROOT)
        record_dir = os.path.dirname(path)
        target = record.get("target") or {}

        # Identity / provenance fields.
        if record.get("schema") != RECORD_SCHEMA:
            errors.append(f"record_schema:{rel}")
        if int(record.get("version", 0) or 0) != RECORD_VERSION:
            errors.append(f"record_version:{rel}")
        for field in ("run_id", "stage", "source", "artifact", "command",
                      "started_at", "finished_at", "exit_status", "completed",
                      "scenarios", "counts", "files"):
            if field not in record:
                errors.append(f"missing_field:{rel}:{field}")
        if record.get("completed") is not True:
            errors.append(f"incomplete:{rel}")
        if int(record.get("exit_status", 1) or 0) != 0:
            errors.append(f"exit_status:{rel}")
        run_id = str(record.get("run_id") or "")
        if not run_id or run_id not in record_dir.split(os.sep):
            errors.append(f"run_id_mismatch:{rel}")
        if not record.get("trusted") and "trusted" in record:
            pass  # clean untrusted lanes are fine; trusted is checked globally

        started = parse_utc(record.get("started_at"))
        finished = parse_utc(record.get("finished_at"))
        if started is None or finished is None:
            errors.append(f"bad_time:{rel}")
        else:
            if finished < started:
                errors.append(f"bad_time_order:{rel}")
            if (finished - started).total_seconds() > MAX_RUN_SECONDS:
                errors.append(f"long_run:{rel}")
            if started > now + datetime.timedelta(minutes=10):
                errors.append(f"future_time:{rel}")

        # Target version match (normalized), actual strings retained.
        if str(target.get("os", "")).lower() != str(required.get("os", "")).lower():
            errors.append(f"version_mismatch:{rel}:os")
        if normalize_version(target.get("anki")) != normalize_version(required.get("anki")):
            errors.append(f"version_mismatch:{rel}:anki")
        if normalize_version(target.get("qt")) != normalize_version(required.get("qt")):
            errors.append(f"version_mismatch:{rel}:qt")
        for field in ("arch", "python", "anki_actual"):
            if not target.get(field):
                errors.append(f"missing_target_field:{rel}:{field}")

        # Artifact provenance.
        artifact = record.get("artifact") or {}
        if manifest is not None:
            if str(artifact.get("file", "")) != str(manifest.get("archive")):
                errors.append(f"artifact_name_mismatch:{rel}")
            if str(artifact.get("sha256", "")) != dist_hash:
                errors.append(f"artifact_hash_mismatch:{rel}")
            if str(artifact.get("flavor", "")) != "ankiaddon":
                errors.append(f"artifact_flavor:{rel}")
            if int(artifact.get("bytes", 0) or 0) <= 0:
                errors.append(f"artifact_bytes:{rel}")
        if merged := artifact.get("sha256"):
            hashes.add(str(merged))
        source = record.get("source") or {}
        if str(source.get("commit", "")):
            commits.add(str(source.get("commit")))

        # Scenarios for this stage + target.
        scenarios = record.get("scenarios") or []
        seen = {}
        for item in scenarios:
            if isinstance(item, dict):
                seen[str(item.get("id"))] = item
        required_scenarios = _required_scenarios(matrix, stage, key[0])
        if not required_scenarios:
            errors.append(f"no_scenarios:{rel}")
        for sid in required_scenarios:
            item = seen.get(sid)
            if item is None:
                errors.append(f"missing_scenario:{rel}:{sid}")
                continue
            status = str(item.get("status", ""))
            if status == "fail":
                errors.append(f"failed_scenario:{rel}:{sid}")
            elif status in ("skip", "blocked", "not-applicable"):
                errors.append(f"skipped_scenario:{rel}:{sid}")
            elif status != "pass":
                errors.append(f"unknown_status:{rel}:{sid}:{status}")
            elif not item.get("assertions"):
                errors.append(f"missing_assertions:{rel}:{sid}")

        # Counts must show real work; zero tests or skipped required work fails.
        counts = record.get("counts") or {}
        passed = int(counts.get("tests_passed", 0) or 0)
        failed = int(counts.get("tests_failed", 0) or 0)
        skipped = int(counts.get("tests_skipped", 0) or 0)
        if passed + failed + skipped <= 0:
            errors.append(f"zero_tests:{rel}")
        if skipped > 0:
            errors.append(f"skipped_tests:{rel}:{skipped}")

        # Files exist, parse, hash-match and were produced during this run.
        for entry in record.get("files") or []:
            if not isinstance(entry, dict):
                errors.append(f"bad_file_entry:{rel}")
                continue
            file_path = os.path.join(record_dir, str(entry.get("path", "")))
            if not os.path.isfile(file_path):
                errors.append(f"file_missing:{rel}:{entry.get('path')}")
                continue
            if sha256_file(file_path) != str(entry.get("sha256", "")):
                errors.append(f"file_hash_mismatch:{rel}:{entry.get('path')}")
            if int(entry.get("bytes", -1)) != os.path.getsize(file_path):
                errors.append(f"file_bytes_mismatch:{rel}:{entry.get('path')}")
            if started and finished:
                mtime = datetime.datetime.fromtimestamp(
                    os.path.getmtime(file_path), datetime.timezone.utc)
                if (mtime < started - datetime.timedelta(seconds=FILE_MTIME_SLACK_S)
                        or mtime > finished + datetime.timedelta(seconds=FILE_MTIME_SLACK_S)):
                    errors.append(f"file_stale_mtime:{rel}:{entry.get('path')}")
            if file_path.endswith(".json"):
                try:
                    load_json(file_path)
                except (OSError, ValueError):
                    errors.append(f"file_unparseable:{rel}:{entry.get('path')}")

        # Metrics vs budgets (nightly/release records that measured them).
        metrics = record.get("metrics") or {}
        if budgets is not None and metrics:
            for failure in evaluate_budgets(metrics, budgets,
                                            enforce_samples=enforce_samples):
                if ":target_missed:" in failure:
                    continue  # target miss is reported in summary, not a gate
                errors.append(f"{failure}:{rel}")
            summary_target = (metrics.get("late_retraction_rebuild") or {}).get("max_ms")
            if summary_target is not None:
                summary.setdefault("late_retraction_rebuild_ms", {})[key[0]] = summary_target
        summary["targets"][f"{key[0]}/{key[1]}/qt{key[2]}"] = "ok"

    # Cross-record provenance: one candidate, one source commit.
    if len(hashes) > 1:
        errors.append(f"mixed_provenance:artifact_sha256:{len(hashes)}")
    if len(commits) > 1:
        errors.append(f"mixed_provenance:source_commit:{len(commits)}")

    # Trusted-lane scenarios appear at least once (not per target).
    trusted_required = [str(s.get("id")) for s in matrix.get("scenarios", [])
                        if stage in (s.get("stages") or [])
                        and str(s.get("host")) == "trusted"]
    if trusted_required:
        present = set()
        for _path, record in records:
            if not record.get("trusted"):
                continue
            for item in record.get("scenarios") or []:
                if isinstance(item, dict) and str(item.get("status")) == "pass" \
                        and item.get("assertions"):
                    present.add(str(item.get("id")))
        for sid in trusted_required:
            if sid not in present:
                errors.append(f"missing_trusted_scenario:{sid}")

    return (not errors), errors, summary


# ------------------------------------------------------------------- verify

def _workflow_for(stage: str, matrix: Dict[str, Any]) -> str:
    return str((matrix.get("workflows") or {}).get(stage, f".github/workflows/{stage}.yml"))


def _scenario_coverage(records, matrix: Dict[str, Any], stage: str) -> set:
    """Scenario ids satisfiable from evidence records for one stage."""
    required_targets = {target_key(t) for t in matrix.get("required_targets", [])}
    satisfied = set()
    per_target = {}
    for _path, record in records:
        if not record:
            continue
        key = target_key(record.get("target") or {})
        trusted = bool(record.get("trusted"))
        passing = {str(item.get("id")) for item in (record.get("scenarios") or [])
                   if isinstance(item, dict) and str(item.get("status")) == "pass"
                   and item.get("assertions")}
        for scenario in matrix.get("scenarios", []):
            if stage not in (scenario.get("stages") or []):
                continue
            sid = str(scenario.get("id"))
            if sid not in passing:
                continue
            host = str(scenario.get("host", "any"))
            if host == "targets":
                per_target.setdefault(sid, set()).add(key)
            elif host == "trusted":
                if trusted:
                    satisfied.add(sid)
            elif host.startswith("linux"):
                if key[0] == "linux":
                    satisfied.add(sid)
            else:
                satisfied.add(sid)
    for sid, keys in per_target.items():
        if required_targets and required_targets <= keys:
            satisfied.add(sid)
    return satisfied


def _host_available(host: str, ctx: Dict[str, Any]) -> bool:
    if host == "any":
        return True
    if host == "targets":
        return True
    if host == "trusted":
        return bool(ctx.get("trusted"))
    if host in ("linux", "linux-docker", "linux-native"):
        if str(ctx.get("os")) != "linux":
            return False
        if host == "linux-docker":
            try:
                return subprocess.run(["docker", "info"], capture_output=True,
                                      timeout=30).returncode == 0
            except (OSError, subprocess.SubprocessError):
                return False
        if host == "linux-native":
            return bool(ctx.get("anki_bin"))
        return True
    return False


def _run_command(cmd: List[str], log_path: str, *, env=None,
                 timeout: Optional[int] = None) -> Tuple[int, str]:
    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"$ {' '.join(cmd)}\n")
        log.flush()
        try:
            proc = subprocess.run(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                  text=True, env=env, timeout=timeout)
            return int(proc.returncode), log_path
        except subprocess.TimeoutExpired:
            log.write("\n[reliability] command timed out\n")
            return 124, log_path
        except OSError as exc:
            log.write(f"\n[reliability] could not execute: {exc!r}\n")
            return 127, log_path


def _parse_unittest_counts(log_path: str) -> Dict[str, int]:
    counts = {"tests_passed": 0, "tests_failed": 0, "tests_skipped": 0}
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return counts
    match = re.search(r"Ran (\d+) tests?", text)
    if match:
        total = int(match.group(1))
        skipped_match = re.search(r"skipped=(\d+)", text)
        skipped = int(skipped_match.group(1)) if skipped_match else 0
        failed = 0
        failures_match = re.search(r"failures=(\d+)", text)
        if failures_match:
            failed += int(failures_match.group(1))
        errors_match = re.search(r"errors=(\d+)", text)
        if errors_match:
            failed += int(errors_match.group(1))
        counts["tests_skipped"] = skipped
        counts["tests_failed"] = failed
        counts["tests_passed"] = max(0, total - failed - skipped)
    return counts


def _dist_version() -> str:
    try:
        return str(load_json(os.path.join(DIST_DIR, "manifest.json")).get("version", ""))
    except (OSError, ValueError):
        return ""


def _expand_command(command: List[str], ctx: Dict[str, Any]) -> List[str]:
    return [sys.executable if part == "python" else part for part in command]


def run_scenario(ctx: Dict[str, Any], scenario: Dict[str, Any],
                 stage: str, out_dir: str) -> Dict[str, Any]:
    """Execute one scenario row; returns its record entry."""
    sid = str(scenario.get("id"))
    host = str(scenario.get("host", "any"))
    entry: Dict[str, Any] = {"id": sid, "status": "blocked", "command": "",
                             "exit_status": None, "detail": "", "assertions": [],
                             "counts": {}}
    if not _host_available(host, ctx):
        entry["detail"] = f"host {host} unavailable on this machine"
        return entry

    log_path = os.path.join(out_dir, f"{sid}.log")
    command = _expand_command(list(scenario.get("command") or []), ctx)
    if sid == "package-audit":
        return _run_package_audit(ctx, out_dir)
    if sid == "performance":
        return _run_performance(ctx, scenario, stage, out_dir)
    if sid == "native-matrix":
        return _run_native_matrix(ctx, out_dir)
    if sid == "native-journeys":
        return _run_native_journeys(ctx, out_dir)
    if sid == "native-smoke":
        return _run_native_smoke(ctx, out_dir)
    if sid == "endurance-30m":
        return _run_endurance(ctx, 30, out_dir)
    if sid == "endurance-2h":
        return _run_endurance(ctx, 120, out_dir)
    if sid == "mutation-gate":
        script = os.path.join(ROOT, "dev", "mutation_gate.py")
        command = [sys.executable, script, "--json", os.path.join(out_dir, "mutation.json")]
    if not command:
        entry["detail"] = "no command defined"
        return entry
    if command[1:2] and command[0] == sys.executable:
        target = command[1]
        if target and not os.path.isabs(target) and not target.startswith("-") \
                and not os.path.exists(os.path.join(ROOT, target.split(" ")[0])):
            entry["detail"] = f"missing scenario tool: {target}"
            return entry
    entry["command"] = " ".join(command)
    rc, log_path = _run_command(command, log_path,
                                env=ctx.get("env"), timeout=ctx.get("timeout"))
    entry["exit_status"] = rc
    entry["status"] = "pass" if rc == 0 else "fail"
    counts = _parse_unittest_counts(log_path) if scenario.get("counts") == "unittest" \
        else {}
    entry["counts"] = counts
    entry["assertions"] = [{"name": sid, "ok": rc == 0,
                            "detail": "" if rc == 0 else f"exit {rc}"}]
    entry["detail"] = f"exit {rc}"
    return entry


def _run_package_audit(ctx: Dict[str, Any], out_dir: str) -> Dict[str, Any]:
    log_path = os.path.join(out_dir, "package-audit.log")
    steps = []
    rc, _ = _run_command([sys.executable, os.path.join(ROOT, "scripts", "build_addon.py"),
                          "--check"], log_path, env=ctx.get("env"))
    steps.append(("build_addon_check", rc))
    if rc == 0:
        try:
            manifest, archive, digest = resolve_artifact()
            audit = os.path.join(ROOT, "scripts", "audit_assets.py")
            if os.path.exists(audit):
                rc2, _ = _run_command(
                    [sys.executable, audit, "--archive", archive], log_path,
                    env=ctx.get("env"))
                steps.append(("archive_audit", rc2))
            else:
                steps.append(("archive_audit", 0))
            steps.append(("artifact_hash", 0 if digest else 1))
            steps.append(("artifact_version", 0 if manifest.get("version") else 1))
        except ReliabilityError:
            steps.append(("artifact_resolve", 1))
    ok = all(rc == 0 for _name, rc in steps)
    return {"id": "package-audit", "status": "pass" if ok else "fail",
            "command": "build_addon.py --check + audit_assets.py --archive",
            "exit_status": 0 if ok else 1, "detail": str(steps),
            "assertions": [{"name": name, "ok": rc == 0} for name, rc in steps],
            "counts": {}}


def _run_performance(ctx: Dict[str, Any], scenario: Dict[str, Any],
                     stage: str, out_dir: str) -> Dict[str, Any]:
    metrics_path = os.path.join(out_dir, "metrics.json")
    profile = "release" if stage == "release" else "nightly"
    command = [sys.executable, os.path.join(ROOT, "dev", "perf_runtime.py"),
               "--json", metrics_path, "--profile", profile]
    log_path = os.path.join(out_dir, "performance.log")
    rc, _ = _run_command(command, log_path, env=ctx.get("env"),
                         timeout=ctx.get("perf_timeout", 3600))
    entry = {"id": "performance", "command": " ".join(command),
             "exit_status": rc, "status": "pass" if rc == 0 else "fail",
             "detail": f"exit {rc}",
             "assertions": [{"name": "perf_runtime", "ok": rc == 0}], "counts": {}}
    if os.path.isfile(metrics_path):
        try:
            metrics = load_json(metrics_path)
            ctx["metrics"] = metrics
            budgets_path = os.path.join(ROOT, "dev", "reliability-budgets.json")
            failures = evaluate_budgets(metrics, load_json(budgets_path),
                                        enforce_samples=stage == "release")
            entry["assertions"].append(
                {"name": "budgets", "ok": not failures, "detail": "; ".join(failures)[:500]})
            if failures:
                entry["status"] = "fail"
                entry["detail"] = "; ".join(failures)[:500]
        except (OSError, ValueError) as exc:
            entry["status"] = "fail"
            entry["detail"] = f"metrics unreadable: {exc!r}"
    return entry


def _e2e_journey_result(ctx: Dict[str, Any], journey: str, out_dir: str) -> Dict[str, Any]:
    import importlib.util

    dev_path = os.path.join(ROOT, "dev.py")
    spec = importlib.util.spec_from_file_location("ankiscape_dev_reliability", dev_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        rc = module._e2e_suite(str(ctx.get("anki", "")), str(ctx.get("qt", "")),
                               journey, str(ctx.get("anki_bin", "") or ""))
    except Exception as exc:  # noqa: BLE001
        rc = 1
        print(f"[reliability] journey {journey} raised {exc!r}", file=sys.stderr)
    base = os.path.join(module.DEV_DIR, "e2e", journey)
    assertions = {}
    try:
        assertions = load_json(os.path.join(base, "e2e-assertions.json"))
    except (OSError, ValueError):
        assertions = {}
    steps = assertions.get("steps", [])
    failed = [s for s in steps if not s.get("ok")]
    shots = []
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        if name.startswith("e2e-") and name.endswith(".png"):
            src = os.path.join(base, name)
            dest = os.path.join(out_dir, f"{journey}-{name[len('e2e-'):]}")
            try:
                shutil.copyfile(src, dest)
                shots.append(dest)
            except OSError:
                pass
    entry = {"journey": journey, "exit_status": int(rc),
             "steps": len(steps), "failed": [s.get("name") for s in failed],
             "screenshots": shots}
    return entry


def _run_native_matrix(ctx: Dict[str, Any], out_dir: str) -> Dict[str, Any]:
    import importlib.util

    if not ctx.get("anki_bin"):
        return {"id": "native-matrix", "status": "blocked", "command": "",
                "exit_status": None, "detail": "no Anki binary on this lane",
                "assertions": [], "counts": {}}
    ui_verify = os.path.join(ROOT, "dev", "ui_ux_verify.py")
    if not os.path.exists(ui_verify):
        return {"id": "native-matrix", "status": "blocked", "command": "",
                "exit_status": None, "detail": "dev/ui_ux_verify.py missing",
                "assertions": [], "counts": {}}
    command = [sys.executable, ui_verify, "--anki", str(ctx.get("anki")),
               "--qt", str(ctx.get("qt"))]
    if ctx.get("anki_bin"):
        command += ["--anki-bin", str(ctx["anki_bin"])]
    log_path = os.path.join(out_dir, "native-matrix.log")
    rc, _ = _run_command(command, log_path, env=ctx.get("env"),
                         timeout=ctx.get("native_timeout", 7200))
    assertions = []
    report = os.path.join(ROOT, "artifacts", "ui-ux",
                          f"report-{ctx.get('anki')}-qt{ctx.get('qt')}.md")
    if os.path.isfile(report):
        assertions.append({"name": "ui_ux_report", "ok": rc == 0,
                           "detail": os.path.relpath(report, ROOT)})
    return {"id": "native-matrix", "status": "pass" if rc == 0 else "fail",
            "command": " ".join(command), "exit_status": rc,
            "detail": f"exit {rc}",
            "assertions": assertions or [{"name": "native-matrix", "ok": rc == 0}],
            "counts": {}}


def _run_native_journeys(ctx: Dict[str, Any], out_dir: str) -> Dict[str, Any]:
    if not ctx.get("anki_bin"):
        return {"id": "native-journeys", "status": "blocked", "command": "",
                "exit_status": None, "detail": "no Anki binary on this lane",
                "assertions": [], "counts": {}}
    results = [_e2e_journey_result(ctx, journey, out_dir) for journey in NEW_JOURNEYS]
    ok = all(item["exit_status"] == 0 and not item["failed"] for item in results)
    return {"id": "native-journeys", "status": "pass" if ok else "fail",
            "command": "dev._e2e_suite x " + ",".join(NEW_JOURNEYS),
            "exit_status": 0 if ok else 1,
            "detail": "; ".join(f"{r['journey']}:{r['exit_status']}" for r in results),
            "assertions": [{"name": r["journey"], "ok": r["exit_status"] == 0
                            and not r["failed"], "detail": ",".join(r["failed"])}
                           for r in results],
            "counts": {}, "journeys": results}


def _run_native_smoke(ctx: Dict[str, Any], out_dir: str) -> Dict[str, Any]:
    entry = _e2e_journey_result(ctx, "fresh", out_dir)
    ok = entry["exit_status"] == 0 and not entry["failed"]
    return {"id": "native-smoke", "status": "pass" if ok else "fail",
            "command": "dev._e2e_suite fresh",
            "exit_status": 0 if ok else 1, "detail": f"fresh:{entry['exit_status']}",
            "assertions": [{"name": "fresh", "ok": ok,
                            "detail": ",".join(entry["failed"])}],
            "counts": {}, "journeys": [entry]}


def _run_endurance(ctx: Dict[str, Any], minutes: int, out_dir: str) -> Dict[str, Any]:
    script = os.path.join(ROOT, "dev", "endurance.py")
    if not os.path.exists(script):
        return {"id": f"endurance-{minutes}m", "status": "blocked", "command": "",
                "exit_status": None, "detail": "dev/endurance.py missing",
                "assertions": [], "counts": {}}
    json_path = os.path.join(out_dir, f"endurance-{minutes}m.json")
    command = [sys.executable, script, "--minutes", str(minutes), "--json", json_path]
    log_path = os.path.join(out_dir, f"endurance-{minutes}m.log")
    rc, _ = _run_command(command, log_path, env=ctx.get("env"),
                         timeout=minutes * 60 + 1800)
    return {"id": f"endurance-{minutes}m", "status": "pass" if rc == 0 else "fail",
            "command": " ".join(command), "exit_status": rc, "detail": f"exit {rc}",
            "assertions": [{"name": "endurance", "ok": rc == 0}], "counts": {}}


# --------------------------------------------------------------- run-lane

def _probe_macos_anki(anki: str) -> Tuple[str, str]:
    """Return (version, binary) for a matching /Applications bundle."""
    import plistlib

    candidates = []
    for name in sorted(os.listdir("/Applications")):
        if name == "Anki.app" or (name.startswith("Anki ") and name.endswith(".app")):
            candidates.append(os.path.join("/Applications", name))
    for cand in candidates:
        exe = os.path.join(cand, "Contents", "MacOS", "Anki")
        if not os.path.isfile(exe):
            continue
        try:
            with open(os.path.join(cand, "Contents", "Info.plist"), "rb") as fh:
                version = plistlib.load(fh).get("CFBundleShortVersionString", "")
        except OSError:
            version = ""
        if not anki or version_matches(anki, version):
            return str(version), exe
    return "", ""


def cmd_run_lane(args) -> int:
    stage = args.stage
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    target_os = (args.os or platform.system().lower()).lower()
    if target_os == "darwin":
        target_os = "macos"
    started = utc_now()
    record: Dict[str, Any] = {
        "schema": RECORD_SCHEMA, "version": RECORD_VERSION, "kind": "target",
        "stage": stage, "run_id": os.path.basename(os.path.dirname(out_dir)) or
        datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "trusted": bool(args.trusted),
        "target": {"os": target_os, "arch": platform.machine(),
                   "anki": args.anki, "anki_actual": args.anki_actual or args.anki,
                   "qt": str(args.qt), "qt_actual": args.qt_actual or str(args.qt),
                   "python": platform.python_version()},
        "source": git_source(),
        "command": f"python3 dev/reliability.py run-lane --stage {stage} --out {args.out}",
        "started_at": started, "finished_at": "", "exit_status": 1,
        "completed": False, "scenarios": [], "counts": {},
        "metrics": {}, "files": [],
    }
    try:
        manifest, archive, digest = resolve_artifact()
        record["artifact"] = {"flavor": "ankiaddon", "file": manifest.get("archive"),
                              "sha256": digest, "bytes": os.path.getsize(archive),
                              "manifest_sha256": sha256_file(
                                  os.path.join(DIST_DIR, "manifest.json"))}
    except ReliabilityError as exc:
        print(f"[reliability] artifact unavailable: {exc}", file=sys.stderr)

    anki_bin = args.anki_bin
    if target_os == "macos" and not anki_bin:
        actual, anki_bin = _probe_macos_anki(args.anki)
        if actual:
            record["target"]["anki_actual"] = actual
    ctx = {"os": target_os, "anki": args.anki, "anki_bin": anki_bin,
           "qt": args.qt, "trusted": bool(args.trusted), "env": os.environ.copy(),
           "timeout": None, "native_timeout": getattr(args, "native_timeout", 7200)}

    matrix = load_json(args.matrix)
    results = []
    for scenario in matrix.get("scenarios", []):
        if stage not in (scenario.get("stages") or []):
            continue
        host = str(scenario.get("host", "any"))
        if host in ("targets", "any", "trusted") or (
                host.startswith("linux") and target_os == "linux") or (
                host == "targets"):
            results.append(run_scenario(ctx, scenario, stage, out_dir))
    record["scenarios"] = results
    record["metrics"] = ctx.get("metrics", {}) or {}
    if record["metrics"]:
        metrics_path = os.path.join(out_dir, "metrics.json")
        if os.path.isfile(metrics_path):
            record["metrics"] = load_json(metrics_path)

    counts = {"tests_passed": 0, "tests_failed": 0, "tests_skipped": 0}
    for item in results:
        item_counts = item.get("counts") or {}
        for key in counts:
            counts[key] += int(item_counts.get(key, 0) or 0)
    record["counts"] = counts
    failed = [item for item in results if item.get("status") != "pass"]
    record["exit_status"] = 0 if not failed else 1
    record["completed"] = True
    record["finished_at"] = utc_now()

    files = []
    for name in sorted(os.listdir(out_dir)):
        path = os.path.join(out_dir, name)
        if not os.path.isfile(path) or name == "record.json":
            continue
        files.append({"path": name, "sha256": sha256_file(path),
                      "bytes": os.path.getsize(path)})
    record["files"] = files
    record_path = os.path.join(out_dir, "record.json")
    with open(record_path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
    for item in failed:
        print(f"[reliability] lane scenario failed: {item.get('id')}: "
              f"{item.get('detail')}", file=sys.stderr)
    print(f"[reliability] lane record at {record_path} "
          f"({'PASS' if not failed else 'FAIL'})")
    return 0 if not failed else 1


# ---------------------------------------------------------------- baseline

def cmd_baseline(args) -> int:
    out_dir = os.path.abspath(args.out or os.path.join(DEFAULT_EVIDENCE, "baseline"))
    os.makedirs(out_dir, exist_ok=True)
    started = utc_now()
    record: Dict[str, Any] = {
        "schema": RECORD_SCHEMA, "version": RECORD_VERSION, "kind": "baseline",
        "stage": "baseline", "run_id": "baseline",
        "target": dict(system_info()), "source": git_source(),
        "command": "python3 dev/reliability.py baseline",
        "started_at": started, "finished_at": "", "exit_status": 1,
        "completed": False, "scenarios": [], "counts": {}, "metrics": {}, "files": [],
    }
    try:
        manifest, archive, digest = resolve_artifact()
        record["artifact"] = {"flavor": "ankiaddon", "file": manifest.get("archive"),
                              "sha256": digest, "bytes": os.path.getsize(archive),
                              "manifest_sha256": sha256_file(
                                  os.path.join(DIST_DIR, "manifest.json"))}
    except ReliabilityError as exc:
        print(f"[reliability] baseline: artifact unavailable: {exc}", file=sys.stderr)

    steps = []
    perf_json = os.path.join(out_dir, "perf_bench.json")
    bench = subprocess.run([sys.executable, os.path.join(ROOT, "dev", "perf_bench.py")],
                           capture_output=True, text=True, cwd=ROOT)
    with open(os.path.join(out_dir, "perf_bench.log"), "w", encoding="utf-8") as fh:
        fh.write(bench.stdout + "\n" + bench.stderr)
    if bench.stdout.strip():
        try:
            text = bench.stdout
            start = text.find("{")
            if start >= 0:
                summary, _end = json.JSONDecoder().raw_decode(text, start)
                with open(perf_json, "w", encoding="utf-8") as fh:
                    json.dump(summary, fh, indent=2)
        except ValueError:
            pass
    steps.append({"id": "perf_bench", "status": "pass" if bench.returncode == 0 else "fail",
                  "command": "python3 dev/perf_bench.py"},
                 )

    runtime_json = os.path.join(out_dir, "perf_runtime.json")
    perf = subprocess.run(
        [sys.executable, os.path.join(ROOT, "dev", "perf_runtime.py"),
         "--json", runtime_json, "--profile", "baseline"], cwd=ROOT)
    steps.append({"id": "perf_runtime_baseline",
                  "status": "pass" if perf.returncode == 0 else "fail",
                  "command": "python3 dev/perf_runtime.py --profile baseline"})
    if os.path.isfile(runtime_json):
        try:
            record["metrics"] = load_json(runtime_json).get("metrics", {})
        except (OSError, ValueError):
            pass

    record["scenarios"] = steps
    failed = [s for s in steps if s["status"] != "pass"]
    record["exit_status"] = 0 if not failed else 1
    record["completed"] = True
    record["finished_at"] = utc_now()
    files = []
    for name in sorted(os.listdir(out_dir)):
        path = os.path.join(out_dir, name)
        if os.path.isfile(path) and name != "record.json":
            files.append({"path": name, "sha256": sha256_file(path),
                          "bytes": os.path.getsize(path)})
    record["files"] = files
    record_path = os.path.join(out_dir, "record.json")
    with open(record_path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
    print(f"[reliability] baseline captured at {record_path}")
    return 0 if not failed else 1


# ------------------------------------------------------------------- verify

def cmd_verify(args) -> int:
    stage = args.stage
    matrix = load_json(args.matrix)
    ctx = {"os": platform.system().lower(), "anki_bin": "", "trusted": bool(args.trusted),
           "env": os.environ.copy(), "timeout": None}
    if ctx["os"] == "darwin":
        ctx["os"] = "macos"
    _actual, anki_bin = _probe_macos_anki("") if ctx["os"] == "macos" else ("", "")
    ctx["anki_bin"] = anki_bin
    run_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_root = os.path.abspath(args.evidence or DEFAULT_EVIDENCE)
    out_dir = os.path.join(out_root, run_id, "local")
    os.makedirs(out_dir, exist_ok=True)

    print(f"[reliability] verify stage={stage} run={run_id} on {ctx['os']}")
    local_results = []
    unavailable = []
    for scenario in matrix.get("scenarios", []):
        if stage not in (scenario.get("stages") or []):
            continue
        host = str(scenario.get("host", "any"))
        if _host_available(host, ctx):
            result = run_scenario(ctx, scenario, stage, out_dir)
            local_results.append(result)
            print(f"[reliability] {scenario.get('id')}: {result['status']} "
                  f"({result.get('detail', '')})")
        else:
            unavailable.append(scenario)

    evidence_errors: List[str] = []
    workflow = _workflow_for(stage, matrix)
    satisfied = set()
    if unavailable:
        if os.path.isdir(out_root):
            records = _collect_records(out_root)
            satisfied = _scenario_coverage(records, matrix, stage)
            if stage in ("nightly", "release"):
                ok, errors, _summary = validate_evidence(
                    args.matrix, out_root,
                    budgets_path=DEFAULT_BUDGETS
                    if os.path.isfile(DEFAULT_BUDGETS) else None,
                    stage=stage, enforce_samples=stage == "release")
                if not ok:
                    evidence_errors.extend(errors)
        for scenario in unavailable:
            sid = str(scenario.get("id"))
            if sid not in satisfied:
                evidence_errors.append(
                    f"scenario_not_covered:{sid}:run it in {workflow} and pass --evidence")
        if not os.path.isdir(out_root):
            evidence_errors.append(f"evidence_missing:{out_root}:{workflow}")

    failures = [r for r in local_results if r.get("status") != "pass"]
    overall = 1 if (failures or evidence_errors) else 0
    for err in evidence_errors:
        print(f"[reliability] {err}", file=sys.stderr)

    summary = {"run_id": run_id, "stage": stage, "os": ctx["os"],
               "local": local_results,
               "unavailable": [str(s.get("id")) for s in unavailable],
               "coverage_from_evidence": sorted(satisfied),
               "evidence_errors": evidence_errors, "overall": overall}
    summary_path = os.path.join(out_dir, f"verify-{stage}.json")
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
    print(f"[reliability] verify {'PASS' if overall == 0 else 'FAIL'}; {summary_path}")
    return overall


def cmd_verify_evidence(args) -> int:
    budgets = args.budgets if os.path.isfile(args.budgets) else None
    ok, errors, summary = validate_evidence(
        args.matrix, args.evidence, budgets_path=budgets, stage=args.stage,
        enforce_samples=args.stage == "release")
    if errors:
        for err in errors:
            print(f"[reliability] evidence: {err}", file=sys.stderr)
    if ok:
        print(f"[reliability] evidence ok: {summary.get('records')} records, "
              f"{len(summary.get('targets', {}))} targets")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="reliability.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_verify = sub.add_parser("verify")
    p_verify.add_argument("--stage", required=True, choices=STAGES)
    p_verify.add_argument("--matrix", default=DEFAULT_MATRIX)
    p_verify.add_argument("--evidence", default=DEFAULT_EVIDENCE)
    p_verify.add_argument("--trusted", action="store_true")
    p_ve = sub.add_parser("verify-evidence")
    p_ve.add_argument("--matrix", default=DEFAULT_MATRIX)
    p_ve.add_argument("--evidence", default=DEFAULT_EVIDENCE)
    p_ve.add_argument("--budgets", default=DEFAULT_BUDGETS)
    p_ve.add_argument("--stage", default="release", choices=STAGES)
    p_lane = sub.add_parser("run-lane")
    p_lane.add_argument("--stage", required=True, choices=STAGES)
    p_lane.add_argument("--out", required=True)
    p_lane.add_argument("--matrix", default=DEFAULT_MATRIX)
    p_lane.add_argument("--os", default="")
    p_lane.add_argument("--anki", default="")
    p_lane.add_argument("--anki-actual", default="")
    p_lane.add_argument("--anki-bin", default="")
    p_lane.add_argument("--qt", default="6")
    p_lane.add_argument("--qt-actual", default="")
    p_lane.add_argument("--trusted", action="store_true")
    p_lane.add_argument("--native-timeout", type=int, default=7200)
    p_base = sub.add_parser("baseline")
    p_base.add_argument("--out", default="")
    args = parser.parse_args(argv)
    if args.cmd == "verify":
        return cmd_verify(args)
    if args.cmd == "verify-evidence":
        return cmd_verify_evidence(args)
    if args.cmd == "run-lane":
        return cmd_run_lane(args)
    if args.cmd == "baseline":
        return cmd_baseline(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
