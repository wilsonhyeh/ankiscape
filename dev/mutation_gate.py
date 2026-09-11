#!/usr/bin/env python3
# dev/mutation_gate.py - Targeted mutation gate (dev only; never shipped).
"""Injects one deliberate defect at a time into a TEMP COPY of the repo and
proves the owning test fails. The working tree is never mutated; after each
case the original file hash is re-verified.

  python3 dev/mutation_gate.py --environment pure
  python3 dev/mutation_gate.py --environment full   # + backend/native lanes

Each case runs a control first (pristine temp copy must pass the owning
command). A failing control means the environment cannot run that lane: the
case is reported blocked, never passed. Every injected defect must make its
owning tests fail; a surviving mutant fails the gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXCLUDE_DIRS = {".git", ".dev", "artifacts", "dist", "__pycache__",
                ".pytest_cache", ".mypy_cache", "node_modules"}

MUTATIONS: List[Dict[str, Any]] = [
    {
        "id": "reward_policy_guard",
        "file": "evolved/engine.py",
        "old": "        if reward_policy not in (1, 2):",
        "new": "        if False:  # mutation",
        "owning": [sys.executable, "-m", "unittest",
                   "tests.test_coverage_guards.PolicyGuardTests"],
        "environment": "pure",
        "description": "unknown reward policies must be rejected",
    },
    {
        "id": "gem_grant",
        "file": "evolved/reducer.py",
        "old": '            if gem_spec:\n                out["gem_out"] = gem_spec["display"]',
        "new": '            if False:  # mutation\n                out["gem_out"] = gem_spec["display"]',
        "owning": [sys.executable, "-m", "unittest",
                   "tests.test_reducer.TestReducer."
                   "test_gem_drop_grants_item_and_bonus_xp"],
        "environment": "pure",
        "description": "a successful gem roll must reach the Bank",
    },
    {
        "id": "stale_generation_guard",
        "file": "evolved/ui/stale.py",
        "old": "    return bool(closed) or int(request_id) != int(current_request_id)",
        "new": "    return False  # mutation",
        "owning": [sys.executable, "-m", "unittest",
                   "tests.test_coverage_guards.StaleGuardTests"],
        "environment": "pure",
        "description": "superseded/closed async responses must be discarded",
    },
    {
        "id": "cohort_filter",
        "file": "server/supabase/migrations/0006_test_cohorts.sql",
        "old": "        and p.is_test = p_is_test",
        "new": "        and true  -- mutation",
        "owning": [sys.executable, "dev.py", "test", "--suite", "backend"],
        "environment": "backend",
        "description": "public ranking paths must exclude the test cohort",
    },
]


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_repo(dest: str) -> None:
    def _ignore(dirpath, names):
        return [n for n in names if n in EXCLUDE_DIRS or n.endswith(".pyc")]

    shutil.copytree(ROOT, dest, ignore=_ignore, dirs_exist_ok=True)


def _run_case(mutation: Dict[str, Any], *, mutate: bool) -> Dict[str, Any]:
    tmp = tempfile.mkdtemp(prefix="ankiscape-mutation-")
    try:
        _copy_repo(tmp)
        target = os.path.join(tmp, mutation["file"])
        if mutate:
            with open(target, encoding="utf-8") as fh:
                text = fh.read()
            if text.count(mutation["old"]) != 1:
                return {"status": "anchor_missing",
                        "detail": f"anchor count="
                                  f"{text.count(mutation['old'])}"}
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(text.replace(mutation["old"], mutation["new"], 1))
        env = dict(os.environ)
        env["PYTHONPATH"] = tmp + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        started = time.monotonic()
        proc = subprocess.run(mutation["owning"], cwd=tmp, env=env,
                              capture_output=True, text=True, timeout=900)
        return {"status": "pass" if proc.returncode == 0 else "fail",
                "returncode": proc.returncode,
                "seconds": round(time.monotonic() - started, 1),
                "stderr": (proc.stderr or "")[-400:],
                "stdout": (proc.stdout or "")[-400:]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mutation_gate.py")
    parser.add_argument("--environment", default="pure",
                        choices=("pure", "full"))
    parser.add_argument("--json", default="")
    parser.add_argument("--only", default="",
                        help="comma-separated mutation ids")
    args = parser.parse_args(argv)
    only = {m for m in args.only.split(",") if m}
    results = []
    failures = 0
    for mutation in MUTATIONS:
        if only and mutation["id"] not in only:
            continue
        if mutation["environment"] != "pure" and args.environment != "full":
            results.append({"id": mutation["id"], "status": "deferred",
                            "environment": mutation["environment"]})
            print(f"mutation_gate: {mutation['id']}: deferred "
                  f"({mutation['environment']} lane; use --environment full)")
            continue
        before = _sha256(os.path.join(ROOT, mutation["file"]))
        control = _run_case(mutation, mutate=False)
        if control["status"] != "pass":
            results.append({"id": mutation["id"], "status": "blocked",
                            "detail": control})
            print(f"mutation_gate: {mutation['id']}: BLOCKED (control "
                  f"{control.get('status')}: "
                  f"{control.get('stderr', '')[-120:]!r})")
            failures += 1
            continue
        mutated = _run_case(mutation, mutate=True)
        after = _sha256(os.path.join(ROOT, mutation["file"]))
        if after != before:
            results.append({"id": mutation["id"], "status": "tree_modified"})
            print(f"mutation_gate: {mutation['id']}: FAIL (working tree "
                  f"modified)", file=sys.stderr)
            failures += 1
            continue
        killed = mutated["status"] == "fail"
        results.append({"id": mutation["id"],
                        "status": "killed" if killed else "survived",
                        "control": control, "mutated": mutated})
        if killed:
            print(f"mutation_gate: {mutation['id']}: killed "
                  f"({mutation['description']})")
        else:
            print(f"mutation_gate: {mutation['id']}: SURVIVED "
                  f"({mutation['description']})", file=sys.stderr)
            failures += 1
    report = {"environment": args.environment, "results": results,
              "failures": failures, "at": int(time.time())}
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    if failures:
        return 1
    print(f"mutation_gate: PASS ({len(results)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
