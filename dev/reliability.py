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
RECORD_VERSION = 2
STAGES = ("pr", "nightly", "release")
ROLES = ("shared", "backend", "native", "hosted")
FILE_MTIME_SLACK_S = 600
MAX_RUN_SECONDS = 48 * 3600

# Native journeys run on every target lane (hosted login/leaderboard coverage
# is required only on the current Qt6 target per OS; see the matrix).
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

def _num(value: Any, default: float = 1e9) -> float:
    """Float coercion that treats None/invalid as "not measured"."""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


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
            if _num(entry.get("p95_ms")) > _num(hook["limit"]):
                _fail("accepted_answer_hook", f"p95:{size}:{entry.get('p95_ms')}")
            if _num(entry.get("p99_ms")) > _num(hook["p99_limit"]):
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
        elif _num(ratio) > _num(cfg["warm_review_scaling"]["limit"]):
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
            if _num(entry.get("p95_ms")) > _num(cfg["reward_completion"]["limit"]):
                _fail("reward_completion", f"p95:{entry.get('p95_ms')}")

    # late_retraction_rebuild: max of runs against hard gate (target reported).
    if "late_retraction_rebuild" in cfg:
        entry = metrics.get("late_retraction_rebuild") or {}
        if not entry:
            if "late_retraction_rebuild" in required:
                _fail("late_retraction_rebuild", "not_measured")
        else:
            worst = _num(entry.get("max_ms"))
            hard = _num(cfg["late_retraction_rebuild"]["hard_limit"])
            if worst > hard:
                _fail("late_retraction_rebuild", f"max:{worst}")
            elif worst > _num(cfg["late_retraction_rebuild"]["target"]):
                failures.append(
                    f"budget:late_retraction_rebuild:target_missed:{worst}")

    # Native-only budgets: enforced when measured, required when named.
    lag = metrics.get("event_loop_lag")
    if lag:
        if _num(lag.get("p95_ms")) > _num(cfg["event_loop_lag"]["limit"]):
            _fail("event_loop_lag", f"p95:{lag.get('p95_ms')}")
        if _num(lag.get("max_ms"), 0.0) > _num(cfg["event_loop_lag"]["max_limit_ms"]):
            _fail("event_loop_lag", f"max:{lag.get('max_ms')}")
    elif "event_loop_lag" in required:
        _fail("event_loop_lag", "not_measured")
    shell = metrics.get("warm_shell_open")
    if shell:
        if _num(shell.get("p95_ms")) > _num(cfg["warm_shell_open"]["limit"]):
            _fail("warm_shell_open", f"p95:{shell.get('p95_ms')}")
    elif "warm_shell_open" in required:
        _fail("warm_shell_open", "not_measured")
    cold = metrics.get("cold_shell_appearance")
    if cold:
        if _num(cold.get("ms")) > _num(cfg["cold_shell_appearance"]["limit"]):
            _fail("cold_shell_appearance", f"ms:{cold.get('ms')}")
    elif "cold_shell_appearance" in required:
        _fail("cold_shell_appearance", "not_measured")
    idle = metrics.get("idle_cpu")
    if idle:
        if _num(idle.get("cpu_pct_delta")) > _num(cfg["idle_cpu"]["limit"]):
            _fail("idle_cpu", f"delta:{idle.get('cpu_pct_delta')}")
        if idle.get("network_polling") not in (None, "none", "existing_scheduled_sync"):
            _fail("idle_cpu", f"network_polling:{idle.get('network_polling')}")
        if idle.get("new_recurring_timer"):
            _fail("idle_cpu", "new_recurring_timer")
    elif "idle_cpu" in required:
        _fail("idle_cpu", "not_measured")
    mem = metrics.get("endurance_memory")
    if mem:
        if _num(mem.get("slope_mib_per_min")) > _num(
                cfg["endurance_memory"]["limit"]):
            _fail("endurance_memory", f"slope:{mem.get('slope_mib_per_min')}")
        if _num(mem.get("settled_increase_mib")) > _num(
                cfg["endurance_memory"]["settled_limit_mib"]):
            _fail("endurance_memory", f"settled:{mem.get('settled_increase_mib')}")
    elif "endurance_memory" in required:
        _fail("endurance_memory", "not_measured")
    return failures


# ----------------------------------------------------------------- validate

def _check(condition: bool, errors: List[str], message: str) -> bool:
    if not condition:
        errors.append(message)
    return condition


def _role_inventory(matrix: Dict[str, Any], role: str, stage: str) -> Dict[str, Any]:
    spec = dict((matrix.get("roles") or {}).get(role) or {})
    spec["job"] = str((spec.get("jobs") or {}).get(stage, ""))
    spec["workflow"] = str((spec.get("workflows") or {}).get(stage, ""))
    return spec


def _scenarios_for_role(matrix: Dict[str, Any], role: str,
                        stage: str) -> List[Dict[str, Any]]:
    return [s for s in matrix.get("scenarios", [])
            if str(s.get("role")) == role and stage in (s.get("stages") or [])]


def _required_scenarios(matrix: Dict[str, Any], stage: str,
                        target_os: str) -> List[str]:
    """Scenario ids required for one native target lane at this stage."""
    _ = target_os
    return [str(s.get("id"))
            for s in _scenarios_for_role(matrix, "native", stage)]


def _target_matches_spec(spec: Dict[str, Any], key: Tuple[str, str, str]) -> bool:
    want = target_key(spec or {})
    return want == key


def _normalize_key(key: Tuple[str, str, str]) -> Tuple[str, str, str]:
    return (str(key[0]).lower(), normalize_version(key[1]), normalize_version(key[2]))


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


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return not (math.isnan(float(value)) or math.isinf(float(value)))


def _metric_sanity(metrics: Dict[str, Any], rel: str) -> List[str]:
    """Reject NaN/infinity, wrong types and contradictory summaries."""
    errors: List[str] = []

    def bad(path: str, detail: str) -> None:
        errors.append(f"metric_invalid:{rel}:{path}:{detail}")

    summaries = []
    for key in ("reward_completion", "warm_shell_open", "event_loop_lag",
                "idle_cpu"):
        entry = metrics.get(key)
        if isinstance(entry, dict):
            summaries.append((key, entry))
    hook = metrics.get("accepted_answer_hook")
    if isinstance(hook, dict):
        for size, entry in hook.items():
            if isinstance(entry, dict):
                summaries.append((f"accepted_answer_hook.{size}", entry))
    for name, entry in summaries:
        samples = entry.get("samples")
        if samples is not None and (not isinstance(samples, int) or samples < 0):
            bad(name + ".samples", repr(samples))
        for field in ("min_ms", "p50_ms", "p95_ms", "p99_ms", "max_ms"):
            if field in entry and not _finite_number(entry[field]):
                bad(f"{name}.{field}", repr(entry[field]))
        order = [entry.get(f) for f in ("min_ms", "p50_ms", "p95_ms", "p99_ms",
                                        "max_ms")]
        vals = [v for v in order if _finite_number(v)]
        if len(vals) > 1 and vals != sorted(vals):
            bad(name + ".order", str(order))
    lag = metrics.get("event_loop_lag")
    if isinstance(lag, dict):
        if _finite_number(lag.get("p95_ms")) and _finite_number(lag.get("max_ms")) \
                and lag["p95_ms"] > lag["max_ms"]:
            bad("event_loop_lag.order", "p95 > max")
    ratio = metrics.get("warm_review_scaling")
    if isinstance(ratio, dict):
        value = ratio.get("p95_ratio_100000_over_1000")
        if value is not None and not _finite_number(value):
            bad("warm_review_scaling.ratio", repr(value))
    mem = metrics.get("endurance_memory")
    if isinstance(mem, dict):
        for field in ("slope_mib_per_min", "settled_increase_mib",
                      "duration_min", "baseline_rss_mib"):
            if field in mem and mem[field] is not None \
                    and not _finite_number(mem[field]):
                bad(f"endurance_memory.{field}", repr(mem[field]))
    rb = metrics.get("late_retraction_rebuild")
    if isinstance(rb, dict):
        for field in ("max_ms", "samples"):
            if field in rb and rb[field] is not None \
                    and not _finite_number(rb[field]):
                bad(f"late_retraction_rebuild.{field}", repr(rb[field]))
    return errors


def _validate_files(record_dir: str, rel: str, entries: List[Any],
                    *, started, finished) -> List[str]:
    errors: List[str] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            errors.append(f"bad_file_entry:{rel}")
            continue
        raw_path = str(entry.get("path", ""))
        if not raw_path or os.path.isabs(raw_path) or ".." in raw_path.split("/"):
            errors.append(f"unsafe_file_path:{rel}:{raw_path}")
            continue
        file_path = os.path.join(record_dir, raw_path)
        if not os.path.isfile(file_path):
            errors.append(f"file_missing:{rel}:{raw_path}")
            continue
        if sha256_file(file_path) != str(entry.get("sha256", "")):
            errors.append(f"file_hash_mismatch:{rel}:{raw_path}")
        if int(entry.get("bytes", -1)) != os.path.getsize(file_path):
            errors.append(f"file_bytes_mismatch:{rel}:{raw_path}")
        if started and finished:
            mtime = datetime.datetime.fromtimestamp(
                os.path.getmtime(file_path), datetime.timezone.utc)
            if (mtime < started - datetime.timedelta(seconds=FILE_MTIME_SLACK_S)
                    or mtime > finished + datetime.timedelta(seconds=FILE_MTIME_SLACK_S)):
                errors.append(f"file_stale_mtime:{rel}:{raw_path}")
        if file_path.endswith(".json"):
            try:
                load_json(file_path)
            except (OSError, ValueError):
                errors.append(f"file_unparseable:{rel}:{raw_path}")
    return errors


def validate_evidence(matrix_path: str, evidence_dir: str, *,
                      budgets_path: Optional[str] = None, stage: str = "release",
                      dist_dir: str = DIST_DIR,
                      enforce_samples: bool = True,
                      expected_commit: str = "",
                      expected_artifact_sha256: str = "",
                      expected_run_id: str = "") -> Tuple[bool, List[str], Dict[str, Any]]:
    """Validate evidence records against the role matrix. Pure and testable.

    Expected inputs bind the evidence set to one candidate: commit, artifact
    hash and run id are not inferred from the evidence itself.
    """
    errors: List[str] = []
    summary: Dict[str, Any] = {"stage": stage, "targets": {}, "records": 0,
                               "roles": {}, "run_id": expected_run_id}

    if not os.path.isfile(matrix_path):
        return False, [f"matrix_missing:{matrix_path}"], summary
    try:
        matrix = load_json(matrix_path)
    except (OSError, ValueError) as exc:
        return False, [f"matrix_unreadable:{exc!r}"], summary
    if int(matrix.get("version", 0) or 0) < 2 or not matrix.get("roles"):
        return False, ["matrix_outdated:requires_version_2_roles"], summary
    required_targets = list(matrix.get("required_targets") or [])
    if not required_targets:
        return False, ["matrix_empty:required_targets"], summary

    if not os.path.isdir(evidence_dir):
        return False, [f"evidence_missing:{evidence_dir}"], summary
    all_records = _collect_records(evidence_dir)
    if not all_records:
        return False, [f"no_records:{evidence_dir}"], summary

    # Select exactly one run: an explicitly expected run id, or (for local
    # maintainer runs) the single run directory present.
    top_dirs = sorted({os.path.relpath(path, evidence_dir).split(os.sep)[0]
                       for path, _record in all_records})
    if expected_run_id:
        if expected_run_id not in top_dirs:
            errors.append(f"run_missing:{expected_run_id}")
        records = [(path, record) for path, record in all_records
                   if os.path.relpath(path, evidence_dir).split(os.sep)[0]
                   == expected_run_id]
    else:
        if len(top_dirs) != 1:
            errors.append(f"multiple_runs:{top_dirs}")
        records = list(all_records)
    if not records:
        return False, errors + [f"no_records_in_run:{expected_run_id or '-'}"], summary
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

    by_role: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
    by_target: Dict[Tuple[str, str, str], List[Tuple[str, Dict[str, Any]]]] = {}
    commits = set()
    hashes = set()
    run_ids = set()

    for path, record in records:
        if not record:
            errors.append(f"record_unreadable:{os.path.relpath(path, ROOT)}")
            continue
        role = str(record.get("role") or "")
        rel = os.path.relpath(path, ROOT)
        record_dir = os.path.dirname(path)
        started = parse_utc(record.get("started_at"))
        finished = parse_utc(record.get("finished_at"))
        target = record.get("target") or {}

        by_role.setdefault(role, []).append((path, record))
        if role == "native":
            by_target.setdefault(target_key(target), []).append((path, record))

        if record.get("schema") != RECORD_SCHEMA:
            errors.append(f"record_schema:{rel}")
        if int(record.get("version", 0) or 0) != RECORD_VERSION:
            errors.append(f"record_version:{rel}")
        for field in ("run_id", "stage", "source", "artifact", "command",
                      "started_at", "finished_at", "exit_status", "completed",
                      "scenarios", "counts", "files", "role", "job"):
            if field not in record:
                errors.append(f"missing_field:{rel}:{field}")
        if record.get("completed") is not True:
            errors.append(f"incomplete:{rel}")
        if int(record.get("exit_status", 1) or 0) != 0:
            errors.append(f"exit_status:{rel}")
        run_id = str(record.get("run_id") or "")
        run_ids.add(run_id)
        if not run_id or run_id not in record_dir.split(os.sep):
            errors.append(f"run_id_mismatch:{rel}")
        if expected_run_id and run_id != expected_run_id:
            errors.append(f"run_id_expected:{rel}:{run_id}!={expected_run_id}")

        if record.get("stage") != stage:
            errors.append(f"stage_mismatch:{rel}:{record.get('stage')}")
        if role not in ROLES:
            errors.append(f"record_role:{rel}:{role!r}")
            continue
        inventory = _role_inventory(matrix, role, stage)
        if stage not in ((matrix.get("roles") or {}).get(role, {})
                         .get("stages") or []):
            errors.append(f"role_stage:{rel}:{role}:{stage}")
        if not inventory.get("job"):
            errors.append(f"role_job:{rel}:{role}:{stage}")
        elif str(record.get("job") or "") != inventory["job"]:
            errors.append(f"job_mismatch:{rel}:{record.get('job')}"
                          f"!={inventory['job']}")
        if role == "hosted" and record.get("trusted") is not True:
            errors.append(f"untrusted_role:{rel}:{role}")

        started_ok = started is not None and finished is not None
        if not started_ok:
            errors.append(f"bad_time:{rel}")
        else:
            if finished < started:
                errors.append(f"bad_time_order:{rel}")
            if (finished - started).total_seconds() > MAX_RUN_SECONDS:
                errors.append(f"long_run:{rel}")
            if started > now + datetime.timedelta(minutes=10):
                errors.append(f"future_time:{rel}")

        source = record.get("source") or {}
        commit = str(source.get("commit", ""))
        if not commit:
            errors.append(f"missing_source_commit:{rel}")
        commits.add(commit)
        if source.get("dirty") is not False:
            errors.append(f"dirty_source:{rel}")
        if expected_commit and commit != expected_commit:
            errors.append(f"commit_expected:{rel}:{commit}!={expected_commit}")

        artifact = record.get("artifact") or {}
        artifact_sha = str(artifact.get("sha256", ""))
        hashes.add(artifact_sha)
        if manifest is not None:
            if str(artifact.get("file", "")) != str(manifest.get("archive")):
                errors.append(f"artifact_name_mismatch:{rel}")
            if artifact_sha != dist_hash:
                errors.append(f"artifact_hash_mismatch:{rel}")
            if str(artifact.get("flavor", "")) != "ankiaddon":
                errors.append(f"artifact_flavor:{rel}")
            if int(artifact.get("bytes", 0) or 0) <= 0:
                errors.append(f"artifact_bytes:{rel}")
        if expected_artifact_sha256 and artifact_sha != expected_artifact_sha256:
            errors.append(f"artifact_expected:{rel}:"
                          f"{artifact_sha[:12]}!={expected_artifact_sha256[:12]}")

        # Target identity: only native records may claim a native target, and
        # the observed handshake must match the requested target.
        if role == "native":
            if not str(target.get("os", "")).lower():
                errors.append(f"missing_target_field:{rel}:os")
            if not str(target.get("anki", "")):
                errors.append(f"missing_target_field:{rel}:anki")
            if not str(target.get("qt", "")):
                errors.append(f"missing_target_field:{rel}:qt")
            observed = str(target.get("anki_actual", ""))
            if not observed:
                errors.append(f"missing_observed_runtime:{rel}:anki_actual")
            elif not version_matches(target.get("anki"), observed):
                errors.append(f"actual_runtime_mismatch:{rel}:{observed}")
            qt_observed = str(target.get("qt_actual", ""))
            if not qt_observed:
                errors.append(f"missing_observed_runtime:{rel}:qt_actual")
            elif not qt_observed.startswith(str(target.get("qt", ""))):
                errors.append(f"actual_qt_mismatch:{rel}:{qt_observed}")
            if not str(target.get("arch", "")):
                errors.append(f"missing_target_field:{rel}:arch")
            if not str(target.get("python", "")):
                errors.append(f"missing_target_field:{rel}:python")
            if not str(target.get("anki_python", "")):
                errors.append(f"missing_observed_runtime:{rel}:anki_python")
        else:
            if str(target.get("anki", "")) or str(target.get("qt", "")):
                errors.append(f"role_target_impersonation:{rel}:{role}")

        # Scenarios owned by this role at this stage.
        scenarios = record.get("scenarios") or []
        seen: Dict[str, Dict[str, Any]] = {}
        duplicates = sorted({str(item.get("id")) for item in scenarios
                             if isinstance(item, dict)
                             and [str(x.get("id")) for x in scenarios].count(
                                 str(item.get("id"))) > 1})
        if duplicates:
            errors.append(f"duplicate_scenarios:{rel}:{duplicates}")
        for item in scenarios:
            if isinstance(item, dict):
                seen[str(item.get("id"))] = item

        for scenario in _scenarios_for_role(matrix, role, stage):
            sid = str(scenario.get("id"))
            item = seen.get(sid)
            if item is None:
                errors.append(f"missing_scenario:{rel}:{sid}")
                continue
            if str(item.get("status")) != "pass":
                errors.append(f"scenario_status:{rel}:{sid}:{item.get('status')}")
            if int(item.get("exit_status", 1) or 0) != 0:
                errors.append(f"scenario_exit:{rel}:{sid}:{item.get('exit_status')}")
            assertions = item.get("assertions") or []
            if not assertions:
                errors.append(f"missing_assertions:{rel}:{sid}")
            for assertion in assertions:
                if not isinstance(assertion, dict):
                    errors.append(f"bad_assertion:{rel}:{sid}")
                elif assertion.get("ok") is not True:
                    errors.append(f"assertion_failed:{rel}:{sid}:"
                                  f"{assertion.get('name')}")
            min_tests = int(scenario.get("min_tests", 0) or 0)
            if min_tests:
                passed = int((item.get("counts") or {}).get("tests_passed", 0) or 0)
                failed = int((item.get("counts") or {}).get("tests_failed", 0) or 0)
                skipped = int((item.get("counts") or {}).get("tests_skipped", 0) or 0)
                if passed < min_tests:
                    errors.append(f"too_few_tests:{rel}:{sid}:{passed}")
                if failed:
                    errors.append(f"test_failures:{rel}:{sid}:{failed}")
                if skipped:
                    errors.append(f"test_skips:{rel}:{sid}:{skipped}")
            require_journeys = list(scenario.get("require_journeys") or [])
            if require_journeys:
                journeys = {str(j.get("journey")): j
                            for j in item.get("journeys") or []
                            if isinstance(j, dict)}
                for journey in require_journeys:
                    entry = journeys.get(journey)
                    if entry is None:
                        errors.append(f"missing_journey:{rel}:{sid}:{journey}")
                        continue
                    if int(entry.get("exit_status", 1) or 0) != 0:
                        errors.append(f"journey_exit:{rel}:{sid}:{journey}")
                    if entry.get("failed"):
                        errors.append(f"journey_failed:{rel}:{sid}:{journey}:"
                                      f"{entry.get('failed')}")
                    if not entry.get("assertions"):
                        errors.append(f"journey_assertions:{rel}:{sid}:{journey}")
            for spec in scenario.get("target_requirements") or []:
                if role != "native" or not _target_matches_spec(
                        spec.get("target") or {}, target_key(target)):
                    continue
                journey = str(spec.get("journey"))
                journeys = {str(j.get("journey")): j
                            for j in item.get("journeys") or []
                            if isinstance(j, dict)}
                entry = journeys.get(journey) or {}
                names = {str(a.get("name")): a
                         for a in entry.get("assertions") or []
                         if isinstance(a, dict)}
                for name in spec.get("assertions") or []:
                    found = names.get(str(name))
                    if found is None:
                        errors.append(f"missing_hosted_assertion:{rel}:{journey}:"
                                      f"{name}")
                    elif found.get("ok") is not True:
                        errors.append(f"hosted_assertion_failed:{rel}:{journey}:"
                                      f"{name}")
            min_files = int(scenario.get("require_files_min", 0) or 0)
            files = item.get("files") or []
            if len(files) < min_files:
                errors.append(f"scenario_files:{rel}:{sid}:{len(files)}")
            errors.extend(_validate_files(record_dir, f"{rel}:{sid}", files,
                                          started=started if started_ok else None,
                                          finished=finished if started_ok else None))
            summary["roles"].setdefault(role, []).append(sid)

        # Every scenario entry's log files are verified (top-level inventory).
        errors.extend(_validate_files(record_dir, rel, record.get("files") or [],
                                      started=started if started_ok else None,
                                      finished=finished if started_ok else None))

        # Metrics: required for the role/stage even when the object is empty.
        metrics = record.get("metrics")
        if not isinstance(metrics, dict):
            errors.append(f"metrics_missing:{rel}")
            metrics = {}
        required_metrics = sorted({m for scenario in _scenarios_for_role(matrix, role, stage)
                                   for m in (scenario.get("require_metrics") or [])})
        if required_metrics:
            for metric in required_metrics:
                if not metrics.get(metric):
                    errors.append(f"missing_metric:{rel}:{metric}")
        if budgets is not None and metrics:
            for failure in evaluate_budgets(metrics, budgets,
                                            enforce_samples=enforce_samples,
                                            required=set(required_metrics) or None):
                if ":target_missed:" in failure:
                    continue  # target miss is reported, not a gate
                errors.append(f"{failure}:{rel}")
        errors.extend(_metric_sanity(metrics, rel))
        for spec in _scenarios_for_role(matrix, role, stage):
            min_minutes = spec.get("min_duration_min")
            if min_minutes:
                duration = (metrics.get("endurance_memory") or {}).get("duration_min")
                if not _finite_number(duration) or float(duration) < float(min_minutes):
                    errors.append(f"duration_short:{rel}:{spec.get('id')}:"
                                  f"{duration}")
            required_metrics = spec.get("require_metrics") or []
            if required_metrics and any(not metrics.get(m) for m in required_metrics):
                pass  # already reported per record above

        # Counts must show real work when the role owns a counted scenario.
        counted = [s for s in _scenarios_for_role(matrix, role, stage)
                   if s.get("counts") == "unittest"]
        if counted:
            counts = record.get("counts") or {}
            passed = sum(int((item.get("counts") or {}).get("tests_passed", 0) or 0)
                         for item in scenarios if isinstance(item, dict))
            failed = sum(int((item.get("counts") or {}).get("tests_failed", 0) or 0)
                         for item in scenarios if isinstance(item, dict))
            skipped = sum(int((item.get("counts") or {}).get("tests_skipped", 0) or 0)
                          for item in scenarios if isinstance(item, dict))
            if passed + failed + skipped <= 0:
                errors.append(f"zero_tests:{rel}")
            if failed:
                errors.append(f"failed_tests:{rel}:{failed}")
            if skipped:
                errors.append(f"skipped_tests:{rel}:{skipped}")
            _ = counts

    # Role completeness: one record per required role; native per target.
    for role in ROLES:
        role_spec = (matrix.get("roles") or {}).get(role) or {}
        if stage not in (role_spec.get("stages") or []):
            continue
        role_records = by_role.get(role, [])
        if not role_records:
            errors.append(f"missing_role:{role}")
            continue
        if role == "native":
            keys = [target_key(record.get("target") or {})
                    for _path, record in role_records]
            duplicates = sorted({k for k in keys if keys.count(k) > 1})
            if duplicates:
                errors.append(f"duplicate_targets:{duplicates}")
            expected_keys = {_normalize_key(target_key(t)) for t in required_targets}
            found_keys = {_normalize_key(k) for k in keys}
            for key in sorted(expected_keys - found_keys):
                errors.append(f"missing_target:{key}")
            for key in sorted(found_keys - expected_keys):
                errors.append(f"unexpected_target:{key}")
            for key in expected_keys:
                summary["targets"][f"{key[0]}/{key[1]}/qt{key[2]}"] = (
                    "ok" if key in found_keys else "missing")
        else:
            if len(role_records) > 1:
                errors.append(f"duplicate_role:{role}:{len(role_records)}")
            summary["roles"][role] = "ok"

    # Cross-record provenance: one candidate, one source commit, one run.
    if len({h for h in hashes if h}) > 1:
        errors.append(f"mixed_provenance:artifact_sha256:{len(hashes)}")
    if len({c for c in commits if c}) > 1:
        errors.append(f"mixed_provenance:source_commit:{len(commits)}")
    if len({r for r in run_ids if r}) > 1:
        errors.append(f"mixed_provenance:run_id:{len(run_ids)}")

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
    profile = "release" if ctx.get("stage") == "release" else "nightly"
    out: List[str] = []
    for part in command:
        if part == "python":
            out.append(sys.executable)
            continue
        out.append(str(part)
                   .replace("{stage}", str(ctx.get("stage", "")))
                   .replace("{profile}", profile)
                   .replace("{anki_bin}", str(ctx.get("anki_bin", "") or "")))
    return out


def _scenario_files(out_dir: str, names: List[str]) -> List[Dict[str, Any]]:
    files = []
    for name in names:
        path = os.path.join(out_dir, name)
        if os.path.isfile(path):
            files.append({"path": name, "sha256": sha256_file(path),
                          "bytes": os.path.getsize(path)})
    return files


def run_scenario(ctx: Dict[str, Any], scenario: Dict[str, Any],
                 stage: str, out_dir: str) -> Dict[str, Any]:
    """Execute one scenario row; returns its record entry."""
    sid = str(scenario.get("id"))
    role = str(scenario.get("role", ""))
    entry: Dict[str, Any] = {"id": sid, "status": "fail", "command": "",
                             "exit_status": 2, "detail": "", "assertions": [],
                             "counts": {}, "files": []}

    # Named prerequisites: a role missing the stack/runtime/credentials fails
    # explicitly instead of silently skipping its required work.
    if role == "native" and not ctx.get("anki_bin"):
        entry["detail"] = "missing_prerequisite:anki_runtime"
        return entry
    if role == "backend" and not _local_stack_ready():
        entry["detail"] = "missing_prerequisite:local_supabase_stack"
        return entry
    if role == "hosted" and not _hosted_credentials_ready():
        entry["detail"] = "missing_prerequisite:hosted_credentials"
        return entry

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
    if sid == "native-performance":
        return _run_native_performance(ctx, out_dir)
    if sid == "endurance-30m":
        return _run_endurance(ctx, 30, out_dir)
    if sid == "endurance-2h":
        return _run_endurance(ctx, 120, out_dir)
    if sid == "sync-local":
        return _run_sync_local(ctx, out_dir)
    if sid == "mutation-gate":
        return _run_mutation_gate(ctx, out_dir)
    if sid == "hosted-fixtures":
        return _run_hosted_fixtures(ctx, out_dir)
    command = _expand_command(list(scenario.get("command") or []), ctx)
    if not command:
        entry["detail"] = "no command defined"
        return entry
    if command[1:2] and command[0] == sys.executable:
        target = command[1]
        if target and not os.path.isabs(target) and not target.startswith("-") \
                and not os.path.exists(os.path.join(ROOT, target.split(" ")[0])):
            entry["detail"] = f"missing scenario tool: {target}"
            return entry
    log_name = f"{sid}.log"
    log_path = os.path.join(out_dir, log_name)
    command = list(command)
    # Versioned command identity in the log; the record keeps the argv.
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
    entry["files"] = _scenario_files(out_dir, [log_name])
    return entry


def _local_stack_ready() -> bool:
    try:
        proc = subprocess.run(["supabase", "status", "-o", "env"],
                              capture_output=True, text=True,
                              cwd=os.path.join(ROOT, "server"), timeout=60)
        return proc.returncode == 0 and "ANON_KEY" in (proc.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return False


def _hosted_credentials_ready() -> bool:
    return bool(os.environ.get("ANKISCAPE_PROD_URL")
                and os.environ.get("ANKISCAPE_PROD_ANON_KEY"))


def _run_package_audit(ctx: Dict[str, Any], out_dir: str) -> Dict[str, Any]:
    log_path = os.path.join(out_dir, "package-audit.log")
    steps = []
    build_script = os.path.join(ROOT, "scripts", "build_addon.py")
    rc, _ = _run_command([sys.executable, build_script, "--check"], log_path,
                         env=ctx.get("env"))
    steps.append(("build_addon_check", rc))
    if rc == 1:
        # No current artifact in this checkout (PR/local role without a
        # distributed candidate): build one, then re-check. The intentional
        # stale probe is normalized once the rebuild succeeds.
        rc_build, _ = _run_command([sys.executable, build_script], log_path,
                                   env=ctx.get("env"))
        steps.append(("build_addon", rc_build))
        rc_check, _ = _run_command([sys.executable, build_script, "--check"],
                                   log_path, env=ctx.get("env"))
        steps.append(("build_addon_recheck", rc_check))
        if rc_build == 0 and rc_check == 0:
            steps[0] = ("build_addon_stale_rebuilt", 0)
        rc = rc_check
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
    metrics_name = "performance-metrics.json"
    metrics_path = os.path.join(out_dir, metrics_name)
    profile = "release" if stage == "release" else "nightly"
    command = [sys.executable, os.path.join(ROOT, "dev", "perf_runtime.py"),
               "--json", metrics_path, "--profile", profile]
    log_name = "performance.log"
    log_path = os.path.join(out_dir, log_name)
    rc, _ = _run_command(command, log_path, env=ctx.get("env"),
                         timeout=ctx.get("perf_timeout", 3600))
    entry = {"id": "performance", "command": " ".join(command),
             "exit_status": rc, "status": "pass" if rc == 0 else "fail",
             "detail": f"exit {rc}",
             "assertions": [{"name": "perf_runtime", "ok": rc == 0}],
             "counts": {},
             "files": _scenario_files(out_dir, [metrics_name, log_name]),
             "metrics": {}}
    metrics = {}
    if os.path.isfile(metrics_path):
        try:
            payload = load_json(metrics_path)
            metrics = payload.get("metrics", {}) if isinstance(payload, dict) else {}
            entry["metrics"] = metrics
            ctx.setdefault("metrics", {}).update(metrics)
            budgets_path = os.path.join(ROOT, "dev", "reliability-budgets.json")
            required = set(scenario.get("require_metrics") or []) or None
            failures = evaluate_budgets(metrics, load_json(budgets_path),
                                        enforce_samples=stage == "release",
                                        required=required)
            entry["assertions"].append(
                {"name": "budgets", "ok": not failures,
                 "detail": "; ".join(failures)[:500]})
            if failures:
                entry["status"] = "fail"
                entry["detail"] = "; ".join(failures)[:500]
        except (OSError, ValueError) as exc:
            entry["status"] = "fail"
            entry["detail"] = f"metrics unreadable: {exc!r}"
    else:
        entry["status"] = "fail"
        entry["detail"] = "metrics file missing"
    return entry


def _e2e_journey_result(ctx: Dict[str, Any], journey: str, out_dir: str,
                        *, scenario_id: str = "") -> Dict[str, Any]:
    import importlib.util

    dev_path = os.path.join(ROOT, "dev.py")
    spec = importlib.util.spec_from_file_location("ankiscape_dev_reliability", dev_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        rc = module._e2e_suite(str(ctx.get("anki", "")), str(ctx.get("qt", "")),
                               journey, str(ctx.get("anki_bin", "") or ""),
                               str(ctx.get("anki_actual", "") or ""))
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
    runtime = assertions.get("runtime") or {}
    if runtime and not ctx.get("observed_runtime"):
        ctx["observed_runtime"] = runtime
    shot_names = []
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        if name.startswith("e2e-") and name.endswith(".png"):
            src = os.path.join(base, name)
            prefix = f"{scenario_id or journey}-"
            dest_name = f"{prefix}{name[len('e2e-'):]}"
            dest = os.path.join(out_dir, dest_name)
            try:
                shutil.copyfile(src, dest)
                shot_names.append(dest_name)
            except OSError:
                pass
    entry = {"journey": journey, "exit_status": int(rc),
             "steps": len(steps),
             "failed": [s.get("name") for s in failed],
             "assertions": [{"name": str(s.get("name")), "ok": bool(s.get("ok")),
                             "detail": str(s.get("detail", ""))[:200]}
                            for s in steps],
             "screenshots": [os.path.join(out_dir, n) for n in shot_names],
             "files": _scenario_files(out_dir, shot_names),
             "runtime": runtime}
    return entry


def _run_native_matrix(ctx: Dict[str, Any], out_dir: str) -> Dict[str, Any]:
    if not ctx.get("anki_bin"):
        return {"id": "native-matrix", "status": "fail", "command": "",
                "exit_status": 2, "detail": "missing_prerequisite:anki_runtime",
                "assertions": [], "counts": {}, "files": []}
    ui_verify = os.path.join(ROOT, "dev", "ui_ux_verify.py")
    if not os.path.exists(ui_verify):
        return {"id": "native-matrix", "status": "fail", "command": "",
                "exit_status": 2, "detail": "missing tool: dev/ui_ux_verify.py",
                "assertions": [], "counts": {}, "files": []}
    command = [sys.executable, ui_verify, "--anki", str(ctx.get("anki")),
               "--qt", str(ctx.get("qt")), "--group", "native"]
    if ctx.get("anki_bin"):
        command += ["--anki-bin", str(ctx["anki_bin"])]
    log_name = "native-matrix.log"
    log_path = os.path.join(out_dir, log_name)
    rc, _ = _run_command(command, log_path, env=ctx.get("env"),
                         timeout=ctx.get("native_timeout", 7200))
    # Preserve the report and observed runtime identity with the lane.
    report = os.path.join(ROOT, "artifacts", "ui-ux",
                          f"report-{ctx.get('anki')}-qt{ctx.get('qt')}.md")
    observed = os.path.join(ROOT, "artifacts", "ui-ux",
                            f"observed-runtime-{ctx.get('anki')}-qt{ctx.get('qt')}.json")
    files = [log_name]
    if os.path.isfile(report):
        shutil.copyfile(report, os.path.join(out_dir, "native-matrix-report.md"))
        files.append("native-matrix-report.md")
    if os.path.isfile(observed):
        shutil.copyfile(observed, os.path.join(out_dir, "native-matrix-runtime.json"))
        files.append("native-matrix-runtime.json")
        try:
            payload = load_json(observed)
            runtime = (payload or {}).get("observed") or {}
            if runtime and not ctx.get("observed_runtime"):
                ctx["observed_runtime"] = runtime
        except (OSError, ValueError):
            pass
    assertions = [{"name": "ui_ux_report", "ok": rc == 0,
                   "detail": os.path.relpath(report, ROOT)}]
    return {"id": "native-matrix", "status": "pass" if rc == 0 else "fail",
            "command": " ".join(command), "exit_status": rc,
            "detail": f"exit {rc}",
            "assertions": assertions, "counts": {},
            "files": _scenario_files(out_dir, files)}


def _run_native_journeys(ctx: Dict[str, Any], out_dir: str) -> Dict[str, Any]:
    if not ctx.get("anki_bin"):
        return {"id": "native-journeys", "status": "fail", "command": "",
                "exit_status": 2, "detail": "missing_prerequisite:anki_runtime",
                "assertions": [], "counts": {}, "files": []}
    results = [_e2e_journey_result(ctx, journey, out_dir,
                                   scenario_id="native-journeys")
               for journey in NEW_JOURNEYS]
    ok = all(item["exit_status"] == 0 and not item["failed"] for item in results)
    files = []
    for item in results:
        files.extend(item.get("files") or [])
    return {"id": "native-journeys", "status": "pass" if ok else "fail",
            "command": "dev._e2e_suite x " + ",".join(NEW_JOURNEYS),
            "exit_status": 0 if ok else 1,
            "detail": "; ".join(f"{r['journey']}:{r['exit_status']}" for r in results),
            "assertions": [{"name": r["journey"], "ok": r["exit_status"] == 0
                            and not r["failed"], "detail": ",".join(r["failed"])}
                           for r in results],
            "counts": {}, "journeys": results,
            "files": files}


def _run_native_smoke(ctx: Dict[str, Any], out_dir: str) -> Dict[str, Any]:
    if not ctx.get("anki_bin"):
        return {"id": "native-smoke", "status": "fail", "command": "",
                "exit_status": 2, "detail": "missing_prerequisite:anki_runtime",
                "assertions": [], "counts": {}, "files": []}
    entry = _e2e_journey_result(ctx, "fresh", out_dir, scenario_id="native-smoke")
    ok = entry["exit_status"] == 0 and not entry["failed"]
    return {"id": "native-smoke", "status": "pass" if ok else "fail",
            "command": "dev._e2e_suite fresh",
            "exit_status": 0 if ok else 1, "detail": f"fresh:{entry['exit_status']}",
            "assertions": [{"name": "fresh", "ok": ok,
                            "detail": ",".join(entry["failed"])}],
            "counts": {}, "journeys": [entry],
            "files": entry.get("files") or []}


def _run_native_performance(ctx: Dict[str, Any], out_dir: str) -> Dict[str, Any]:
    tool = os.path.join(ROOT, "dev", "native_performance.py")
    if not os.path.exists(tool):
        return {"id": "native-performance", "status": "fail", "command": "",
                "exit_status": 2,
                "detail": "missing tool: dev/native_performance.py",
                "assertions": [], "counts": {}, "files": []}
    metrics_name = "native-performance-metrics.json"
    raw_name = "native-performance-raw.json"
    profile = "release" if ctx.get("stage") == "release" else "nightly"
    command = [sys.executable, tool, "--anki", str(ctx.get("anki")),
               "--qt", str(ctx.get("qt")), "--profile", profile,
               "--anki-bin", str(ctx.get("anki_bin", "") or ""),
               "--json", os.path.join(out_dir, metrics_name),
               "--raw", os.path.join(out_dir, raw_name)]
    log_name = "native-performance.log"
    rc, _ = _run_command(command, os.path.join(out_dir, log_name),
                         env=ctx.get("env"),
                         timeout=ctx.get("native_timeout", 7200))
    entry = {"id": "native-performance", "command": " ".join(command),
             "exit_status": rc, "status": "pass" if rc == 0 else "fail",
             "detail": f"exit {rc}",
             "assertions": [{"name": "native_performance", "ok": rc == 0}],
             "counts": {},
             "files": _scenario_files(out_dir, [metrics_name, raw_name, log_name])}
    metrics_path = os.path.join(out_dir, metrics_name)
    if os.path.isfile(metrics_path):
        try:
            payload = load_json(metrics_path)
            metrics = payload.get("metrics", {}) if isinstance(payload, dict) else {}
            ctx.setdefault("metrics", {}).update(metrics)
            runtime = payload.get("observed") or {}
            if runtime and not ctx.get("observed_runtime"):
                ctx["observed_runtime"] = runtime
        except (OSError, ValueError):
            entry["status"] = "fail"
            entry["detail"] = "metrics unreadable"
    return entry


def _run_sync_local(ctx: Dict[str, Any], out_dir: str) -> Dict[str, Any]:
    entry = _e2e_journey_result(ctx, "sync", out_dir, scenario_id="sync-local")
    ok = entry["exit_status"] == 0 and not entry["failed"]
    return {"id": "sync-local", "status": "pass" if ok else "fail",
            "command": "dev._e2e_suite sync",
            "exit_status": 0 if ok else 1,
            "detail": f"sync:{entry['exit_status']}",
            "assertions": [{"name": "sync", "ok": ok,
                            "detail": ",".join(entry["failed"])}],
            "counts": {}, "journeys": [entry],
            "files": entry.get("files") or []}


def _run_mutation_gate(ctx: Dict[str, Any], out_dir: str) -> Dict[str, Any]:
    script = os.path.join(ROOT, "dev", "mutation_gate.py")
    json_name = "mutation.json"
    log_name = "mutation-gate.log"
    command = [sys.executable, script, "--environment", "full",
               "--json", os.path.join(out_dir, json_name)]
    rc, _ = _run_command(command, os.path.join(out_dir, log_name),
                         env=ctx.get("env"), timeout=ctx.get("perf_timeout", 3600))
    return {"id": "mutation-gate", "status": "pass" if rc == 0 else "fail",
            "command": " ".join(command), "exit_status": rc,
            "detail": f"exit {rc}",
            "assertions": [{"name": "mutation_full", "ok": rc == 0}],
            "counts": {},
            "files": _scenario_files(out_dir, [json_name, log_name])}


def _run_endurance(ctx: Dict[str, Any], minutes: int, out_dir: str) -> Dict[str, Any]:
    tool = os.path.join(ROOT, "dev", "native_performance.py")
    sid = f"endurance-{minutes}m"
    if not os.path.exists(tool):
        return {"id": sid, "status": "fail", "command": "", "exit_status": 2,
                "detail": "missing tool: dev/native_performance.py",
                "assertions": [], "counts": {}, "files": []}
    metrics_name = f"{sid}-metrics.json"
    raw_name = f"{sid}-raw.json"
    profile = "release" if ctx.get("stage") == "release" else "nightly"
    command = [sys.executable, tool, "--anki", str(ctx.get("anki")),
               "--qt", str(ctx.get("qt")), "--profile", profile,
               "--anki-bin", str(ctx.get("anki_bin", "") or ""),
               "--endurance-minutes", str(minutes),
               "--json", os.path.join(out_dir, metrics_name),
               "--raw", os.path.join(out_dir, raw_name)]
    log_name = f"{sid}.log"
    rc, _ = _run_command(command, os.path.join(out_dir, log_name),
                         env=ctx.get("env"), timeout=minutes * 60 + 2700)
    entry = {"id": sid, "status": "pass" if rc == 0 else "fail",
             "command": " ".join(command), "exit_status": rc, "detail": f"exit {rc}",
             "assertions": [{"name": "endurance_native", "ok": rc == 0}],
             "counts": {},
             "files": _scenario_files(out_dir, [metrics_name, raw_name, log_name])}
    metrics_path = os.path.join(out_dir, metrics_name)
    if os.path.isfile(metrics_path):
        try:
            payload = load_json(metrics_path)
            metrics = payload.get("metrics", {}) if isinstance(payload, dict) else {}
            ctx.setdefault("metrics", {}).update(metrics)
        except (OSError, ValueError):
            entry["status"] = "fail"
            entry["detail"] = "metrics unreadable"
    return entry


def _run_hosted_fixtures(ctx: Dict[str, Any], out_dir: str) -> Dict[str, Any]:
    steps = [
        (["dev/seed_hosted_fixtures.py", "--hosted", "--plan"],
         "hosted_fixture_plan", 600),
        (["dev/seed_hosted_fixtures.py", "--hosted", "--apply"],
         "hosted_fixture_apply_1", 1800),
        (["dev/seed_hosted_fixtures.py", "--hosted", "--apply"],
         "hosted_fixture_apply_2_idempotent", 1800),
        (["dev/seed_hosted_fixtures.py", "--hosted", "--verify"],
         "hosted_fixture_verify", 1800),
        (["dev/hosted_fixture_e2e.py", "--hosted"],
         "hosted_fixture_e2e", 1800),
    ]
    assertions = []
    files = []
    for argv, name, timeout in steps:
        command = [sys.executable, os.path.join(ROOT, argv[0])] + argv[1:]
        log_name = f"hosted-fixtures-{name}.log"
        rc, _ = _run_command(command, os.path.join(out_dir, log_name),
                             env=ctx.get("env"), timeout=timeout)
        assertions.append({"name": name, "ok": rc == 0, "detail": f"exit {rc}"})
        files.append(log_name)
    ok = all(a["ok"] for a in assertions)
    return {"id": "hosted-fixtures", "status": "pass" if ok else "fail",
            "command": "dev/seed_hosted_fixtures.py + dev/hosted_fixture_e2e.py",
            "exit_status": 0 if ok else 1,
            "detail": "; ".join(f"{a['name']}:{a['detail']}" for a in assertions),
            "assertions": assertions, "counts": {},
            "files": _scenario_files(out_dir, files)}


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


def _observed_runtime_from(results) -> Dict[str, Any]:
    for item in results or []:
        runtime = item.get("runtime") or {}
        if runtime:
            return runtime
        for journey in item.get("journeys") or []:
            runtime = journey.get("runtime") or {}
            if runtime:
                return runtime
    return {}


def cmd_run_lane(args) -> int:
    stage = args.stage
    role = args.role
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    target_os = (args.os or platform.system().lower()).lower()
    if target_os == "darwin":
        target_os = "macos"
    started = utc_now()
    matrix = load_json(args.matrix)
    inventory = _role_inventory(matrix, role, stage)
    trusted = bool(args.trusted) or role == "hosted"
    record: Dict[str, Any] = {
        "schema": RECORD_SCHEMA, "version": RECORD_VERSION, "kind": "target",
        "stage": stage, "role": role, "job": inventory.get("job", ""),
        "run_id": os.path.basename(os.path.dirname(out_dir)) or
        datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "trusted": trusted,
        "target": {"os": target_os, "arch": platform.machine(),
                   "python": platform.python_version()},
        "source": git_source(),
        "command": f"python3 dev/reliability.py run-lane --stage {stage} "
                   f"--role {role} --out {args.out}",
        "started_at": started, "finished_at": "", "exit_status": 1,
        "completed": False, "scenarios": [], "counts": {},
        "metrics": {}, "files": [],
    }
    if role == "native":
        record["target"].update({"anki": args.anki, "anki_actual": "",
                                 "qt": str(args.qt), "qt_actual": "",
                                 "anki_python": ""})
    try:
        manifest, archive, digest = resolve_artifact()
        record["artifact"] = {"flavor": "ankiaddon", "file": manifest.get("archive"),
                              "sha256": digest, "bytes": os.path.getsize(archive),
                              "manifest_sha256": sha256_file(
                                  os.path.join(DIST_DIR, "manifest.json"))}
    except ReliabilityError as exc:
        print(f"[reliability] artifact unavailable: {exc}", file=sys.stderr)

    anki_bin = args.anki_bin
    if role == "native" and target_os == "macos" and not anki_bin:
        _actual, anki_bin = _probe_macos_anki(args.anki)
    ctx = {"os": target_os, "stage": stage, "role": role, "anki": args.anki,
           "anki_bin": anki_bin,
           "anki_actual": getattr(args, "anki_actual", ""),
           "qt": args.qt, "trusted": trusted, "env": os.environ.copy(),
           "timeout": None, "native_timeout": getattr(args, "native_timeout", 7200)}

    results = []
    for scenario in _scenarios_for_role(matrix, role, stage):
        results.append(run_scenario(ctx, scenario, stage, out_dir))
    record["scenarios"] = results
    record["metrics"] = ctx.get("metrics", {}) or {}
    if role == "native":
        observed = ctx.get("observed_runtime") or _observed_runtime_from(results)
        record["target"].update({
            "anki_actual": str(observed.get("anki", "")),
            "qt_actual": str(observed.get("qt", "")),
            "anki_python": str(observed.get("python", "")),
            "arch": str(observed.get("arch", "")) or platform.machine(),
        })

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

def _role_available_here(role: str, ctx: Dict[str, Any]) -> bool:
    if role == "shared":
        return True
    if role == "backend":
        return ctx.get("os") == "linux" and _local_stack_ready()
    if role == "native":
        return bool(ctx.get("anki_bin"))
    if role == "hosted":
        return bool(ctx.get("trusted")) and _hosted_credentials_ready()
    return False


def cmd_verify(args) -> int:
    stage = args.stage
    matrix = load_json(args.matrix)
    evidence_dir = os.path.abspath(args.evidence or DEFAULT_EVIDENCE)
    aggregation = bool(args.evidence or args.expected_commit
                       or args.expected_artifact_sha256 or args.expected_run_id)
    if aggregation:
        # Aggregation-only: validate the supplied evidence set without
        # executing any scenario.
        ok, errors, summary = validate_evidence(
            args.matrix, evidence_dir,
            budgets_path=DEFAULT_BUDGETS if os.path.isfile(DEFAULT_BUDGETS) else None,
            stage=stage, enforce_samples=stage == "release",
            expected_commit=args.expected_commit,
            expected_artifact_sha256=args.expected_artifact_sha256,
            expected_run_id=args.expected_run_id)
        for err in errors:
            print(f"[reliability] {err}", file=sys.stderr)
        print(f"[reliability] verify aggregation {'PASS' if ok else 'FAIL'} "
              f"({summary.get('records')} records)")
        return 0 if ok else 1

    ctx = {"os": platform.system().lower(), "stage": stage,
           "anki_bin": "", "trusted": bool(args.trusted),
           "env": os.environ.copy(), "timeout": None}
    if ctx["os"] == "darwin":
        ctx["os"] = "macos"
        _actual, anki_bin = _probe_macos_anki("")
        ctx["anki_bin"] = anki_bin
    run_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_root = evidence_dir
    out_dir = os.path.join(out_root, run_id, "local")
    os.makedirs(out_dir, exist_ok=True)

    print(f"[reliability] verify stage={stage} run={run_id} on {ctx['os']}")
    local_results: List[Dict[str, Any]] = []
    errors: List[str] = []
    for role in ROLES:
        scenarios = _scenarios_for_role(matrix, role, stage)
        if not scenarios:
            continue
        if not _role_available_here(role, ctx):
            inventory = _role_inventory(matrix, role, stage)
            errors.append(f"role_unavailable:{role}:run it in "
                          f"{inventory.get('workflow') or 'CI'}")
            continue
        for scenario in scenarios:
            result = run_scenario(ctx, scenario, stage, out_dir)
            local_results.append(result)
            print(f"[reliability] {role}/{scenario.get('id')}: {result['status']} "
                  f"({result.get('detail', '')})")

    failures = [r for r in local_results if r.get("status") != "pass"]
    overall = 1 if (failures or errors) else 0
    for err in errors:
        print(f"[reliability] {err}", file=sys.stderr)

    summary = {"run_id": run_id, "stage": stage, "os": ctx["os"],
               "local": local_results,
               "errors": errors, "overall": overall}
    summary_path = os.path.join(out_dir, f"verify-{stage}.json")
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
    print(f"[reliability] verify {'PASS' if overall == 0 else 'FAIL'}; {summary_path}")
    return overall


def cmd_verify_evidence(args) -> int:
    budgets = args.budgets if os.path.isfile(args.budgets) else None
    errors: List[str] = []
    if args.stage in ("nightly", "release"):
        if not args.expected_commit:
            errors.append("expected_commit_required")
        if not args.expected_artifact_sha256:
            errors.append("expected_artifact_sha256_required")
        if not args.expected_run_id:
            errors.append("expected_run_id_required")
        if errors:
            for err in errors:
                print(f"[reliability] evidence: {err}", file=sys.stderr)
            return 1
    ok, errors, summary = validate_evidence(
        args.matrix, args.evidence, budgets_path=budgets, stage=args.stage,
        enforce_samples=args.stage == "release",
        expected_commit=args.expected_commit,
        expected_artifact_sha256=args.expected_artifact_sha256,
        expected_run_id=args.expected_run_id)
    if errors:
        for err in errors:
            print(f"[reliability] evidence: {err}", file=sys.stderr)
    if ok:
        print(f"[reliability] evidence ok: {summary.get('records')} records, "
              f"{len(summary.get('targets', {}))} targets, run "
              f"{summary.get('run_id') or 'local'}")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="reliability.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_verify = sub.add_parser("verify")
    p_verify.add_argument("--stage", required=True, choices=STAGES)
    p_verify.add_argument("--matrix", default=DEFAULT_MATRIX)
    p_verify.add_argument("--evidence", default="")
    p_verify.add_argument("--trusted", action="store_true")
    p_verify.add_argument("--expected-commit", default="")
    p_verify.add_argument("--expected-artifact-sha256", default="")
    p_verify.add_argument("--expected-run-id", default="")
    p_ve = sub.add_parser("verify-evidence")
    p_ve.add_argument("--matrix", default=DEFAULT_MATRIX)
    p_ve.add_argument("--evidence", default=DEFAULT_EVIDENCE)
    p_ve.add_argument("--budgets", default=DEFAULT_BUDGETS)
    p_ve.add_argument("--stage", default="release", choices=STAGES)
    p_ve.add_argument("--expected-commit", default="")
    p_ve.add_argument("--expected-artifact-sha256", default="")
    p_ve.add_argument("--expected-run-id", default="")
    p_lane = sub.add_parser("run-lane")
    p_lane.add_argument("--stage", required=True, choices=STAGES)
    p_lane.add_argument("--role", required=True, choices=ROLES)
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
