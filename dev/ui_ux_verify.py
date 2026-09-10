#!/usr/bin/env python3
"""dev/ui_ux_verify.py - Real-Anki UI/UX verification across scenarios.

Runs every existing journey plus the 3.0 shell scenarios on a managed
synthetic runtime, checks each result's freshness (run id + artifact hash +
Qt version), collects screenshots into artifacts/ui-ux/, and writes the
inspected report at artifacts/ui-ux/report.md.

Usage:
  python3 dev/ui_ux_verify.py --anki 26.08.1 --qt 6
  python3 dev/ui_ux_verify.py --anki 23.10 --qt 6 --scenarios fresh,ui-training

Exit nonzero on: missing runtime, unknown/skipped scenario, stale artifact or
result, UI exception, or any failed assertion. This never opens a personal
Anki profile and never touches production.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARTIFACTS = os.path.join(ROOT, "artifacts", "ui-ux")

ALL_SCENARIOS = (
    "fresh", "upgrade", "undo", "catchup", "sync", "dialogs",
    "ui-onboarding", "ui-training", "ui-settings", "ui-review",
    "ui-lifecycle",
)
SHELL_SCENARIOS = ("ui-onboarding", "ui-training", "ui-settings",
                   "ui-review", "ui-lifecycle")


def _load_dev():
    path = os.path.join(ROOT, "dev.py")
    spec = importlib.util.spec_from_file_location("ankiscape_dev", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dist_hash() -> str:
    try:
        with open(os.path.join(ROOT, "dist", "manifest.json"),
                  encoding="utf-8") as fh:
            return str(json.load(fh).get("artifact_sha256", ""))
    except (OSError, ValueError):
        return ""


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _run_journey(dev, args, journey):
    try:
        rc = dev._e2e_suite(args.anki, str(args.qt), journey)
    except Exception as exc:  # noqa: BLE001
        rc = 1
        print(f"ui_ux_verify: {journey} raised {exc!r}", file=sys.stderr)
    base = os.path.join(dev.DEV_DIR, "e2e", journey)
    return int(rc), _read_json(os.path.join(base, "e2e-assertions.json"))


def _compose_combined_report() -> None:
    """Merge every per-target report and the static findings into report.md."""
    parts = ["# AnkiScape 3.0 UI/UX verification report", ""]
    findings = os.path.join(ARTIFACTS, "FINDINGS.md")
    if os.path.isfile(findings):
        with open(findings, encoding="utf-8") as fh:
            parts.append(fh.read().rstrip())
        parts.append("")
    for name in sorted(os.listdir(ARTIFACTS)):
        if not name.startswith("report-") or not name.endswith(".md"):
            continue
        with open(os.path.join(ARTIFACTS, name), encoding="utf-8") as fh:
            body = fh.read().rstrip()
        parts.append("---")
        parts.append("")
        parts.append(body)
        parts.append("")
    with open(os.path.join(ARTIFACTS, "report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts).rstrip() + "\n")


def _wait_for_anki_exit(timeout_s: int = 60) -> None:
    """A journey's Anki must fully exit before the next launch; a single
    instance would otherwise swallow -b/-p and risk the personal profile."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            proc = subprocess.run(["pgrep", "-x", "Anki"], capture_output=True,
                                  text=True, timeout=10)
            if not (proc.stdout or "").strip():
                return
        except (OSError, subprocess.TimeoutExpired):
            return
        time.sleep(1)


def _collect_screenshots(base: str, out_dir: str) -> list:
    copied = []
    os.makedirs(out_dir, exist_ok=True)
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return copied
    for name in names:
        if not name.startswith("e2e-") or not name.endswith(".png"):
            continue
        src = os.path.join(base, name)
        dest = os.path.join(out_dir, name[len("e2e-"):])
        try:
            shutil.copyfile(src, dest)
            copied.append(dest)
        except OSError:
            continue
    return copied


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ui_ux_verify.py")
    parser.add_argument("--anki", required=True,
                        help="target Anki version prefix, e.g. 26.08.1")
    parser.add_argument("--qt", required=True, type=int, choices=(5, 6))
    parser.add_argument("--scenarios", default=",".join(ALL_SCENARIOS))
    args = parser.parse_args(argv)

    dev = _load_dev()
    scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    unknown = [s for s in scenarios if s not in ALL_SCENARIOS]
    if unknown:
        print(f"ui_ux_verify: ERROR: unknown scenario(s): {unknown}",
              file=sys.stderr)
        return 2
    missing = [s for s in ALL_SCENARIOS if s not in scenarios]
    if missing:
        # "Skipped scenario" is a failure: the release claim needs all of them.
        print(f"ui_ux_verify: ERROR: scenarios not run: {missing}",
              file=sys.stderr)
        return 2

    app, anki_bin, installed = dev._pick_app(args.anki)
    if not anki_bin:
        print(f"ui_ux_verify: ERROR: no installed Anki.app matches "
              f"{args.anki!r}", file=sys.stderr)
        return 2

    run_stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = os.path.join(ARTIFACTS, f"{args.anki}-qt{args.qt}-{run_stamp}")
    os.makedirs(out_dir, exist_ok=True)

    results = []
    failures = []
    for journey in scenarios:
        print(f"ui_ux_verify: === {journey} (Anki {installed}, Qt {args.qt}) ===")
        _wait_for_anki_exit()
        rc, assertions = _run_journey(dev, args, journey)
        if rc != 0 and assertions is None:
            # Launch races (a lingering process or base handle right after a
            # previous scenario) must not masquerade as a result. Settle and
            # retry once; assertion failures are never retried.
            print(f"ui_ux_verify: {journey}: launch failed early; retrying once")
            _wait_for_anki_exit(120)
            time.sleep(10)
            rc, assertions = _run_journey(dev, args, journey)
        # Compare against the artifact the run itself built (the suite
        # rebuilds dist/ before launching Anki).
        built_hash = _dist_hash()
        base = os.path.join(dev.DEV_DIR, "e2e", journey)
        entry = {"journey": journey, "returncode": int(rc),
                 "screenshots": [], "qt_version": "",
                 "artifact_sha256": built_hash, "steps": 0, "failed": []}
        if rc != 0:
            failures.append(f"{journey}: journey failed (rc={rc})")
        if journey == "sync":
            # The sync journey is a standalone local-stack script (no Anki
            # driver result); rc=0 is the assertion.
            entry["artifact_sha256"] = built_hash
        elif assertions is None:
            failures.append(f"{journey}: no assertions written (stale result)")
        else:
            run_id = assertions.get("run_id")
            run_file = os.path.join(base, "run-id.txt")
            expected_run = ""
            try:
                with open(run_file, encoding="utf-8") as fh:
                    expected_run = fh.read().strip()
            except OSError:
                pass
            if not run_id or run_id != expected_run:
                failures.append(f"{journey}: stale result (run_id mismatch)")
            steps = assertions.get("steps", [])
            failed_steps = [s for s in steps if not s.get("ok")]
            entry["steps"] = len(steps)
            entry["failed"] = [s.get("name") for s in failed_steps]
            if failed_steps:
                failures.append(f"{journey}: failed steps {entry['failed']}")
            if not assertions.get("screenshots"):
                # Shell scenarios must produce visual evidence.
                if journey in SHELL_SCENARIOS:
                    failures.append(f"{journey}: no screenshots")
            qt_version = str(assertions.get("qt_version", ""))
            entry["qt_version"] = qt_version
            if not qt_version.startswith(str(args.qt)):
                failures.append(
                    f"{journey}: Qt mismatch (ran {qt_version!r}, "
                    f"want major {args.qt})")
            recorded_hash = ""
            try:
                with open(os.path.join(base, "artifact_sha256.txt"),
                          encoding="utf-8") as fh:
                    recorded_hash = fh.read().strip()
            except OSError:
                pass
            entry["artifact_sha256"] = recorded_hash or built_hash
            if built_hash and recorded_hash != built_hash:
                failures.append(f"{journey}: stale package "
                                f"({recorded_hash[:12]} != {built_hash[:12]})")
        entry["screenshots"] = _collect_screenshots(
            base, os.path.join(out_dir, journey))
        results.append(entry)
        print(f"ui_ux_verify: {journey}: rc={rc} "
              f"screenshots={len(entry['screenshots'])}")

    # Inspected report with the real evidence: one per target plus a
    # combined report.md that includes the static visual findings.
    report_path = os.path.join(ARTIFACTS, f"report-{args.anki}-qt{args.qt}.md")
    lines = [
        "# AnkiScape 3.0 UI/UX verification report",
        "",
        f"- Run: {run_stamp} (UTC)",
        f"- Anki: {installed} (requested {args.anki})",
        f"- Qt major: {args.qt}",
        f"- Artifact: {_dist_hash()[:16]}...",
        f"- Scenarios: {len(results)} (all required)",
        "",
        "| Scenario | Result | Steps | Failed | Screenshots | Qt |",
        "|---|---|---|---|---|---|",
    ]
    for entry in results:
        ok = entry["returncode"] == 0 and not entry["failed"]
        lines.append(
            f"| {entry['journey']} | {'PASS' if ok else 'FAIL'} "
            f"| {entry['steps']} | {', '.join(entry['failed']) or '-'} "
            f"| {len(entry['screenshots'])} | {entry['qt_version'] or '?'} |")
    lines += ["", "Screenshot directories:"]
    for entry in results:
        lines.append(f"- `artifacts/ui-ux/{os.path.basename(out_dir)}/"
                     f"{entry['journey']}/`")
    if failures:
        lines += ["", "## Failures", ""]
        lines += [f"- {f}" for f in failures]
    else:
        lines += ["", "All scenarios passed on the packaged artifact."]
    os.makedirs(ARTIFACTS, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    _compose_combined_report()

    print(f"ui_ux_verify: report at {report_path}")
    if failures:
        for failure in failures:
            print(f"ui_ux_verify: FAILED: {failure}", file=sys.stderr)
        return 1
    print(f"ui_ux_verify: PASS on Anki {installed} Qt {args.qt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
