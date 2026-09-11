#!/usr/bin/env python3
# scripts/configure_github.py - Idempotent GitHub configuration for this repo.
"""  python3 scripts/configure_github.py --plan
  python3 scripts/configure_github.py --apply
  python3 scripts/configure_github.py --verify

Applies only: labels (creating missing ones), Issues enabled, private
vulnerability reporting (when the API supports it), and verification that the
workflow/issue-form files exist on the target branch. Never changes
visibility, the default branch, repository description or topics, never
creates sample issues, never sends messages. Requires the authorized `gh`
CLI session for --apply/--verify; --plan works offline."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "wilsonhyeh/ankiscape"
TARGET_BRANCH = "main"

LABELS = (
    ("bug", "d73a4a", "Something isn't working"),
    ("performance", "fbca04", "Latency, memory or resource regressions"),
    ("crash", "b60205", "Hard failure or repeated error dialog"),
    ("data-loss", "e11d21", "Progress or game state at risk"),
    ("needs-info", "d4c5f9", "Waiting on reporter details"),
    ("confirmed", "0e8a16", "Reproduced by a maintainer"),
    ("platform:macos", "1d76db", "macOS-specific report"),
    ("platform:windows", "006b75", "Windows-specific report"),
    ("platform:linux", "5319e7", "Linux-specific report"),
)

REQUIRED_FILES = (
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/workflows/pr.yml",
    ".github/workflows/nightly.yml",
    ".github/workflows/release-verify.yml",
    "SUPPORT.md",
    "SECURITY.md",
)
ADVISORY_LINK = "security/advisories/new"


def _gh(args, *, check=False):
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True,
                              timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", repr(exc)
    if check and proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip()[:300])
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _api(path, *, method="GET", body=None, jq="", extra=()):
    args = ["api", path, "--method", method]
    if body is not None:
        for key, value in body.items():
            args += ["-f", f"{key}={value}"]
    if jq:
        args += ["--jq", jq]
    args += list(extra)
    return _gh(args)


def repo_state():
    rc, out, err = _api(f"repos/{REPO}",
                        jq="{visibility,default_branch,has_issues,archived}")
    if rc != 0:
        return None, err
    try:
        return json.loads(out), ""
    except ValueError:
        return None, out[:200]


def label_names():
    rc, out, _err = _api(f"repos/{REPO}/labels", jq=".[].name",
                         extra=["--paginate"])
    if rc != 0:
        return None
    return {line.strip() for line in out.splitlines() if line.strip()}


def pvr_state():
    rc, out, _err = _api(f"repos/{REPO}/private-vulnerability-reporting",
                         jq=".enabled")
    if rc != 0:
        return None
    return out.strip() == "true"


def file_exists(path: str) -> bool:
    rc, _out, _err = _api(f"repos/{REPO}/contents/{path}",
                          extra=["--jq", ".path"])
    return rc == 0


def config_has_advisory() -> bool:
    path = os.path.join(ROOT, ".github", "ISSUE_TEMPLATE", "config.yml")
    try:
        with open(path, encoding="utf-8") as fh:
            return ADVISORY_LINK in fh.read()
    except OSError:
        return False


def inject_advisory_link() -> bool:
    """Security contact link only once private reporting is confirmed on."""
    path = os.path.join(ROOT, ".github", "ISSUE_TEMPLATE", "config.yml")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if ADVISORY_LINK in text:
            return False
        block = ("  - name: Report a vulnerability privately\n"
                 f"    url: https://github.com/{REPO}/{ADVISORY_LINK}\n"
                 "    about: Private security reporting (never a public issue).\n")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.rstrip("\n") + "\n" + block)
        return True
    except OSError:
        return False


def cmd_plan() -> int:
    print(f"configure_github: repo {REPO} (target branch {TARGET_BRANCH})")
    state, err = repo_state()
    if state is None:
        print(f"configure_github: hosted state unavailable ({err[:120]}); "
              "offline plan only")
        state = {}
    else:
        print(f"configure_github: visibility={state.get('visibility')} "
              f"default_branch={state.get('default_branch')} "
              f"issues={state.get('has_issues')} archived={state.get('archived')}")
    have = label_names() if state else None
    missing = [name for name, _c, _d in LABELS if have is None or name not in have]
    print(f"plan: create missing labels: {missing or 'none'}")
    print("plan: keep Issues enabled (no change to visibility/default branch)")
    if state:
        pvr = pvr_state()
        if pvr is None:
            print("plan: private vulnerability reporting unsupported by API")
        elif pvr:
            print("plan: private vulnerability reporting already enabled")
        else:
            print("plan: enable private vulnerability reporting")
    print("plan: verify required files on branch: "
          f"{len(REQUIRED_FILES)} files")
    print("plan: no sample issues, no messages, no visibility changes")
    return 0


def cmd_apply() -> int:
    state, err = repo_state()
    if state is None:
        print(f"configure_github: ERROR: gh api unavailable: {err[:200]}",
              file=sys.stderr)
        return 2
    if str(state.get("visibility")) != "public" or state.get("archived"):
        print("configure_github: ERROR: repository is not the expected public, "
              "non-archived repo; refusing to touch it", file=sys.stderr)
        return 2
    if str(state.get("default_branch")) != TARGET_BRANCH:
        print(f"configure_github: ERROR: default branch is "
              f"{state.get('default_branch')!r}, expected {TARGET_BRANCH!r}",
              file=sys.stderr)
        return 2
    if not state.get("has_issues"):
        rc, _o, e = _api(f"repos/{REPO}", method="PATCH",
                         body={"has_issues": "true"})
        print(f"configure_github: issues enabled ({rc} {e[:80]})")
    have = label_names() or set()
    created = []
    for name, color, description in LABELS:
        if name in have:
            continue
        rc, _out, err = _api(f"repos/{REPO}/labels", method="POST",
                             body={"name": name, "color": color,
                                   "description": description})
        if rc == 0:
            created.append(name)
        else:
            print(f"configure_github: label {name} failed: {err[:120]}",
                  file=sys.stderr)
    print(f"configure_github: labels created: {created or 'none'}")
    pvr = pvr_state()
    if pvr is False:
        rc, _out, err = _api(f"repos/{REPO}/private-vulnerability-reporting",
                             method="PUT")
        print(f"configure_github: private vulnerability reporting enable "
              f"rc={rc}")
        if rc == 0:
            pvr = True
        else:
            print(f"configure_github: {err[:140]}", file=sys.stderr)
    if pvr and not config_has_advisory():
        if inject_advisory_link():
            print("configure_github: config.yml security link added "
                  "(working-tree change; commit/push authorized separately)")
    return cmd_verify()


def cmd_verify() -> int:
    problems = []
    state, err = repo_state()
    if state is None:
        problems.append(f"gh api unavailable: {err[:120]}")
    else:
        if state.get("visibility") != "public":
            problems.append("repository is not public")
        if state.get("default_branch") != TARGET_BRANCH:
            problems.append(f"default branch is {state.get('default_branch')}")
        if not state.get("has_issues"):
            problems.append("issues disabled")
    have = label_names()
    if have is None:
        problems.append("labels unreadable")
    else:
        for name, _c, _d in LABELS:
            if name not in have:
                problems.append(f"missing label {name}")
    pvr = pvr_state()
    if pvr is None:
        print("configure_github: note: private vulnerability reporting API "
              "unsupported; not treated as a failure")
    elif not pvr:
        problems.append("private vulnerability reporting disabled")
    for path in REQUIRED_FILES:
        try:
            exists = file_exists(path)
        except RuntimeError as exc:
            problems.append(f"{path}: {exc}")
            continue
        if not exists:
            problems.append(f"missing file on branch: {path}")
    if problems:
        for problem in problems:
            print(f"configure_github: FAIL {problem}", file=sys.stderr)
        return 1
    print(f"configure_github: verify PASS ({REPO}, "
          f"{len(LABELS)} labels, {len(REQUIRED_FILES)} files)")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="configure_github.py")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    if args.plan:
        return cmd_plan()
    if args.apply:
        return cmd_apply()
    return cmd_verify()


if __name__ == "__main__":
    raise SystemExit(main())
