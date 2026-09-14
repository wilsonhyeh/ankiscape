#!/usr/bin/env python3
# dev/account_journey_e2e.py - Account-journey matrix runner (dev only).
"""Runs the packaged `ui-account-lifecycle` native journey on this machine and
records its evidence as one per-target record, or aggregates previously
exported records.

Modes:
  --local                  run the integrated local Auth -> app -> RPC chain
                           (requires the local Supabase stack; seeds the five
                           public demos first) and write this target's record
  --collect                do not run anything; only aggregate records
  --require-all-targets    fail with a missing-target list unless records for
                           every target in the matrix are present

Record layout (one per target, exportable from its own OS):
  artifacts/account-journey/<os>-<anki>-qt<qt>/record.json

CI supplies Linux/Windows/macOS records; a lone machine that runs
--require-all-targets fails with the missing list instead of pretending the
matrix was covered. Missing targets and failed assertions are failures.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

JOURNEY = "ui-account-lifecycle"
DEFAULT_RECORD_DIR = os.path.join(ROOT, "artifacts", "account-journey")
LOCAL_SECRET = "ankiscape-local-demo-secret-0001"
PLAN_PATH = os.path.join(ROOT, "artifacts", "account-repair",
                         "demo-plan-local.json")


class JourneyError(Exception):
    pass


def _load_dev_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ankiscape_dev_driver", os.path.join(ROOT, "dev.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _supabase_env() -> Dict[str, str]:
    try:
        proc = subprocess.run(["supabase", "status", "-o", "env"],
                              capture_output=True, text=True, timeout=60,
                              cwd=os.path.join(ROOT, "server"))
    except (OSError, subprocess.SubprocessError):
        return {}
    env: Dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        if "=" in line:
            key, _sep, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"')
    if not (env.get("ANON_KEY") or env.get("PUBLISHABLE_KEY")):
        return {}
    return env


def _target_os() -> str:
    system = platform.system().lower()
    return {"darwin": "macos", "windows": "windows",
            "linux": "linux"}.get(system, system)


def _normalize_anki(value: str) -> str:
    """Match the reliability matrix naming: '23.10' keeps its two-part form
    (23.10.1 is a patch of it), while 26.x keeps the patch ('26.08.1' ->
    '26.8.1')."""
    value = str(value or "").strip()
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", value)
    if not match:
        return value
    major, minor, patch = (int(part) if part is not None else None
                           for part in match.groups())
    if (major, minor) == (23, 10):
        return "23.10"
    if patch is None:
        return f"{major}.{minor}"
    return f"{major}.{minor}.{patch}"


def _normalize_qt(value: str) -> str:
    value = str(value or "").strip()
    match = re.match(r"^(\d+)", value)
    return match.group(1) if match else value


def target_key(target: Dict[str, Any]) -> str:
    return (f"{str(target.get('os','')).lower()}-"
            f"{_normalize_anki(target.get('anki',''))}-"
            f"qt{_normalize_qt(target.get('qt',''))}")


def _record_path(record_dir: str, key: str) -> str:
    return os.path.join(record_dir, key, "record.json")


def _run_local(record_dir: str, anki: str, qt: str) -> Dict[str, Any]:
    env = _supabase_env()
    if not env:
        raise JourneyError("local Supabase stack unavailable "
                           "(start Docker Desktop + `supabase start`)")
    anon = env.get("ANON_KEY") or env.get("PUBLISHABLE_KEY", "")
    service = env.get("SERVICE_ROLE_KEY", "")
    if not anon or not service:
        raise JourneyError("local stack keys unavailable")
    run_env = dict(os.environ)
    run_env.update({
        "ANKISCAPE_ACCOUNT_JOURNEY_MODE": "auth",
        "ANKISCAPE_FIXTURE_SECRET": LOCAL_SECRET,
        "ANKISCAPE_LOCAL_URL": env.get("API_URL", ""),
        "ANKISCAPE_LOCAL_ANON_KEY": anon,
        "ANKISCAPE_LOCAL_SERVICE_ROLE_KEY": service,
    })
    # Seed the five public demos first (real replay through the submission
    # RPCs against the local stack).
    subprocess.run([sys.executable, os.path.join(ROOT, "dev",
                                                 "demo_players.py"),
                    "--local", "--plan", "--plan-file", PLAN_PATH],
                   env=run_env, check=True)
    subprocess.run([sys.executable, os.path.join(ROOT, "dev",
                                                 "demo_players.py"),
                    "--local", "--apply", "--plan-file", PLAN_PATH],
                   env=run_env, check=True)
    dev = _load_dev_module()
    rc = dev._e2e_suite(anki, qt, JOURNEY, "", "", phase_timeout_s=1200)
    base = os.path.join(dev.DEV_DIR, "e2e", JOURNEY)
    record: Dict[str, Any] = {"rc": int(rc), "started": time.time()}
    for name, field in (("e2e-assertions.json", "assertions_file"),
                        ("e2e-heartbeat.json", "heartbeat_file"),
                        ("journey.json", "journey_file")):
        path = os.path.join(base, name)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    record[field] = json.load(fh)
            except (OSError, ValueError):
                record[field] = {}
    record["artifact_sha256"] = ""
    try:
        with open(os.path.join(ROOT, "dist", "manifest.json"),
                  encoding="utf-8") as fh:
            record["artifact_sha256"] = json.load(fh).get("artifact_sha256",
                                                          "")
    except (OSError, ValueError):
        pass
    try:
        record["source_sha"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            cwd=ROOT, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        record["source_sha"] = ""
    return record


def _make_record(raw: Dict[str, Any]) -> Dict[str, Any]:
    assertions_file = raw.get("assertions_file") or {}
    runtime = assertions_file.get("runtime") or {}
    target = {
        "os": _target_os(),
        "anki": _normalize_anki(runtime.get("anki", "")),
        "qt": _normalize_qt(runtime.get("qt", "")),
    }
    steps = assertions_file.get("steps") or []
    assertions = {str(step.get("name")): bool(step.get("ok"))
                  for step in steps if isinstance(step, dict)}
    failed = [str(step.get("name")) for step in steps
              if isinstance(step, dict) and not step.get("ok")]
    return {
        "schema": "ankiscape-account-journey-record",
        "version": 1,
        "journey": JOURNEY,
        "target": target,
        "target_key": target_key(target),
        "runtime": runtime,
        "mode": "auth",
        "run_id": assertions_file.get("run_id", ""),
        "exit_code": int(raw.get("rc", 1)),
        "assertions": assertions,
        "failed": failed,
        "source_sha": raw.get("source_sha", ""),
        "artifact_sha256": raw.get("artifact_sha256", ""),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _collect(record_dir: str) -> Dict[str, Dict[str, Any]]:
    records: Dict[str, Dict[str, Any]] = {}
    for path in glob.glob(os.path.join(record_dir, "**", "record.json"),
                          recursive=True):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("schema") != "ankiscape-account-journey-record":
            continue
        key = str(data.get("target_key")
                  or target_key(data.get("target") or {}))
        records[key] = data
    return records


def _required_targets(matrix_path: str) -> tuple:
    """(required targets, per-journey assertion requirements).

    Absence or failure of any assertion a scenario lists for a target's
    journey makes that target's record fail. Per-journey assertion owners
    live on scenario entries (e.g. the native-journeys scenario)."""
    with open(matrix_path, encoding="utf-8") as fh:
        matrix = json.load(fh)
    targets = matrix.get("required_targets")
    if not isinstance(targets, list) or not targets:
        raise JourneyError(f"{matrix_path} has no required_targets")
    top = [entry for entry in targets if isinstance(entry, dict)]
    requirements: List[Dict[str, Any]] = []
    for entry in list(targets) + list(matrix.get("scenarios") or []):
        if not isinstance(entry, dict):
            continue
        for requirement in entry.get("target_requirements") or []:
            if isinstance(requirement, dict):
                requirements.append(requirement)
    return top, requirements


def _validate(records: Dict[str, Dict[str, Any]],
              required: tuple) -> List[str]:
    top_targets, requirements = required
    errors: List[str] = []
    expected = {target_key(target) for target in top_targets}
    for key in sorted(expected - set(records)):
        errors.append(f"missing_target:{key}")
    for key, record in sorted(records.items()):
        if key not in expected:
            errors.append(f"unexpected_target:{key}")
            continue
        if int(record.get("exit_code", 1)) != 0:
            errors.append(f"journey_exit:{key}")
        if record.get("failed"):
            errors.append(f"journey_failed:{key}:{','.join(record['failed'])}")
        assertions = record.get("assertions") or {}
        journey = str(record.get("journey") or "")
        for requirement in requirements:
            if target_key(requirement.get("target") or {}) != key:
                continue
            required_journey = str(requirement.get("journey") or "")
            if required_journey and required_journey != journey:
                continue
            for name in requirement.get("assertions") or []:
                if name not in assertions:
                    errors.append(f"missing_assertion:{key}:{name}")
                elif not assertions[name]:
                    errors.append(f"failed_assertion:{key}:{name}")
    return errors


def _export_local(record_dir: str) -> Dict[str, Any]:
    """Convert the just-run native journey evidence into this target's record
    (no Anki launch, no stack): used by lanes that already ran the journey."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ankiscape_dev_driver_export", os.path.join(ROOT, "dev.py"))
    dev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dev)
    base = os.path.join(dev.DEV_DIR, "e2e", JOURNEY)
    raw: Dict[str, Any] = {"rc": 1, "started": time.time()}
    path = os.path.join(base, "e2e-assertions.json")
    try:
        with open(path, encoding="utf-8") as fh:
            raw["assertions_file"] = json.load(fh)
    except (OSError, ValueError):
        raise JourneyError(f"no journey evidence at {path}; run the "
                           f"ui-account-lifecycle journey first")
    ok = not (raw["assertions_file"].get("errors")
              or [s for s in (raw["assertions_file"].get("steps") or [])
                  if not s.get("ok")])
    raw["rc"] = 0 if ok else 1
    try:
        with open(os.path.join(ROOT, "dist", "manifest.json"),
                  encoding="utf-8") as fh:
            raw["artifact_sha256"] = json.load(fh).get("artifact_sha256", "")
    except (OSError, ValueError):
        raw["artifact_sha256"] = ""
    try:
        raw["source_sha"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            cwd=ROOT, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raw["source_sha"] = ""
    return raw


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--local", action="store_true")
    target.add_argument("--collect", action="store_true")
    target.add_argument("--export", action="store_true",
                        help="convert existing journey evidence into a record")
    parser.add_argument("--matrix", default=os.path.join(
        ROOT, "dev", "reliability-matrix.json"))
    parser.add_argument("--record-dir", default=DEFAULT_RECORD_DIR)
    parser.add_argument("--require-all-targets", action="store_true")
    parser.add_argument("--anki", default="")
    parser.add_argument("--qt", default="")
    parser.add_argument("--no-run", action="store_true",
                        help="validate existing records only")
    args = parser.parse_args(argv)
    try:
        if args.export:
            raw = _export_local(args.record_dir)
            record = _make_record(raw)
            if not record["target"]["anki"]:
                raise JourneyError(
                    "journey did not report a runtime identity; evidence "
                    "cannot be bound to a target")
            path = _record_path(args.record_dir, record["target_key"])
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(record, fh, indent=2, sort_keys=True)
                fh.write("\n")
            print(f"account_journey: wrote {os.path.relpath(path, ROOT)}")
            print(f"account_journey: exit={record['exit_code']} "
                  f"failed={record['failed']}")
        elif args.local and not args.no_run and not args.collect:
            raw = _run_local(args.record_dir, args.anki, args.qt)
            record = _make_record(raw)
            if not record["target"]["anki"]:
                raise JourneyError(
                    "journey did not report a runtime identity; evidence "
                    "cannot be bound to a target")
            path = _record_path(args.record_dir, record["target_key"])
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(record, fh, indent=2, sort_keys=True)
                fh.write("\n")
            print(f"account_journey: wrote {os.path.relpath(path, ROOT)}")
            print(f"account_journey: exit={record['exit_code']} "
                  f"failed={record['failed']}")
        records = _collect(args.record_dir)
        if not args.require_all_targets:
            print(f"account_journey: {len(records)} record(s) collected")
            return 0
        required = _required_targets(args.matrix)
        errors = _validate(records, required)
        if errors:
            for error in errors:
                print(f"account_journey: FAIL {error}", file=sys.stderr)
            print(f"account_journey: FAIL ({len(errors)} finding(s))")
            return 1
        print(f"account_journey: PASS ({len(required[0])} targets)")
        return 0
    except JourneyError as exc:
        print(f"account_journey: BLOCKED: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"account_journey: FAIL: seeding command failed: {exc}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
