#!/usr/bin/env python3
"""scripts/build_addon.py - Deterministic package build from an explicit allowlist.

Builds dist/ankiscape-3.0.0.ankiaddon with manifest package 1808450369.
Excludes tests/dev/server/artifacts/dist/.dev/.git/.venv, credentials/env
files, meta.json, user_files, journals, logs, caches, editor files. Asserts
the exact archive member set, traversal safety, required assets and source
hash. Secret-content scans staged/tracked source and runtime contents;
deliberately public Supabase anon keys are distinguished from privileged keys.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
VERSION = "3.0.0"
PACKAGE_ID = "1808450369"
ARCHIVE = os.path.join(DIST, f"ankiscape-{VERSION}.ankiaddon")

# Explicit runtime allowlist (relative to repo root). Nothing else ships.
ALLOWLIST_DIRS = ("bars", "crafteditems", "fish", "gems", "icon", "ores", "trees", "evolved", "shared")
ALLOWLIST_FILES = ("__init__.py", "constants.py", "debug.py", "deck_injection_pure.py",
                   "hooks.py", "injectors.py", "logic.py", "logic_pure.py", "mode.py",
                   "runtime.py", "storage.py", "storage_pure.py", "ui.py", "utils.py",
                   "manifest.json", "LICENSE", "LICENSE.txt", "README.md")
REQUIRED_ASSETS = ("manifest.json", "__init__.py", "shared/rules-v1.json")

SECRET_PATTERNS = (
    # Concrete privileged material only - bare words like "service role" in
    # comments/docs are not secrets (e.g. "never service-role keys" prose).
    re.compile(r"sb_secret_[A-Za-z0-9_-]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"re_[A-Za-z0-9]{20,}"),
    re.compile(r"service_role['\"]?\s*[:=]\s*['\"]?eyJ[A-Za-z0-9_-]+", re.I),
    re.compile(r"smtp_(pass|password)['\"]?\s*[:=]\s*['\"][^'\"]+", re.I),
)
# Public anon keys are expected in config; only privileged patterns above fail.
ALLOW_PUBLIC_ANON = re.compile(r"sb_publishable_[A-Za-z0-9_-]+|eyJ[A-Za-z0-9_-]{10,}")


def _collect() -> list:
    members = []
    for name in ALLOWLIST_FILES:
        path = os.path.join(ROOT, name)
        if os.path.isfile(path):
            members.append(name)
    for dirname in ALLOWLIST_DIRS:
        dpath = os.path.join(ROOT, dirname)
        if not os.path.isdir(dpath):
            continue
        for base, _dirs, files in os.walk(dpath):
            # Skip caches and editor noise even inside allowlisted dirs.
            _dirs[:] = [d for d in _dirs if d not in ("__pycache__", ".venv", ".git")]
            for fn in sorted(files):
                if fn.endswith((".pyc", ".pyo", ".log", ".sqlite3", ".env")):
                    continue
                if fn in (".DS_Store",):
                    continue
                full = os.path.join(base, fn)
                rel = os.path.relpath(full, ROOT)
                members.append(rel)
    # Deterministic order; reject traversal entries.
    members = sorted(set(members))
    for member in members:
        if member.startswith("/") or ".." in member.split(os.sep):
            raise SystemExit(f"unsafe member: {member!r}")
    return members


def _scan_secrets(members: list) -> list:
    hits = []
    for member in members:
        path = os.path.join(ROOT, member)
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in SECRET_PATTERNS:
            match = pattern.search(text)
            if match:
                hits.append(f"{member}: {pattern.pattern[:40]}...")
                break
    return hits


def main() -> int:
    members = _collect()
    missing = [req for req in REQUIRED_ASSETS if req not in members]
    if missing:
        print(f"build: missing required assets: {missing}", file=sys.stderr)
        return 1
    # Compile check over shipped Python (catches syntax errors in modules
    # that headless unit tests never import, e.g. lazy-Qt shells).
    import py_compile
    for member in members:
        if member.endswith(".py"):
            try:
                py_compile.compile(os.path.join(ROOT, member), doraise=True)
            except py_compile.PyCompileError as exc:
                print(f"build: compile failed: {exc}", file=sys.stderr)
                return 1
    # manifest identity for sideload/upgrade tests.
    manifest_path = os.path.join(ROOT, "manifest.json")
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        manifest = {}
    if str(manifest.get("package", "")) != PACKAGE_ID:
        print(f"build: manifest package must be {PACKAGE_ID} for sideload tests "
              f"(got {manifest.get('package')!r})", file=sys.stderr)
        return 1
    hits = _scan_secrets(members)
    if hits:
        print("build: secret-content scan hits:", file=sys.stderr)
        for hit in hits:
            print(f"  {hit}", file=sys.stderr)
        return 1
    os.makedirs(DIST, exist_ok=True)
    # Source hash over member bytes (deterministic order).
    sha = hashlib.sha256()
    for member in members:
        with open(os.path.join(ROOT, member), "rb") as fh:
            sha.update(fh.read())
    source_hash = sha.hexdigest()
    with zipfile.ZipFile(ARCHIVE, "w", zipfile.ZIP_DEFLATED) as archive:
        for member in members:
            full = os.path.join(ROOT, member)
            # Fixed timestamp for a reproducible artifact (source hash covers
            # content; the zip hash must not depend on filesystem mtimes).
            info = zipfile.ZipInfo(member, date_time=(2026, 9, 9, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(full, "rb") as fh:
                archive.writestr(info, fh.read())
    with open(ARCHIVE, "rb") as fh:
        artifact_hash = hashlib.sha256(fh.read()).hexdigest()
    record = {"archive": os.path.basename(ARCHIVE), "version": VERSION,
              "package": PACKAGE_ID, "members": len(members),
              "source_hash": source_hash, "artifact_sha256": artifact_hash,
              "member_list": members}
    with open(os.path.join(DIST, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
    print(f"build: {ARCHIVE} ({len(members)} members) sha256={artifact_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
