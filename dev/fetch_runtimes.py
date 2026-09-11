#!/usr/bin/env python3
# dev/fetch_runtimes.py - Verified runtime downloads for the native matrix.
"""  python3 dev/fetch_runtimes.py --list
  python3 dev/fetch_runtimes.py --target macos/26.8.1 --dest .dev/runtimes
  python3 dev/fetch_runtimes.py --verify arch.tar.zst --target linux/26.8.1

Fails closed when a target's URL is not marked verified or its SHA-256 pin is
empty: CI must never download an unpinned runtime. `--allow-unpinned` exists
for a maintainer's first local fetch and must not be used in workflows."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "dev", "runtime_manifest.json")


def load_manifest() -> dict:
    with open(MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


def find_target(manifest: dict, target: str) -> dict:
    os_name, _sep, rest = target.partition("/")
    anki = rest
    for entry in manifest["targets"]:
        if entry["os"] == os_name and entry["anki"] == anki:
            return entry
    raise SystemExit(f"fetch_runtimes: unknown target {target!r}")


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive(path: str, entry: dict, *, require_pin: bool = True) -> str:
    if not os.path.isfile(path):
        raise SystemExit(f"fetch_runtimes: archive missing: {path}")
    digest = sha256_file(path)
    pin = str(entry.get("sha256") or "")
    if require_pin and not pin:
        raise SystemExit(
            f"fetch_runtimes: no SHA-256 pin recorded for "
            f"{entry['os']}/{entry['anki']}; a maintainer must record it in "
            f"dev/runtime_manifest.json after verifying the official asset")
    if pin and digest != pin:
        raise SystemExit(
            f"fetch_runtimes: hash mismatch for {entry['os']}/{entry['anki']}: "
            f"{digest[:16]} != {pin[:16]}")
    return digest


def download(entry: dict, dest_dir: str, *, allow_unpinned: bool) -> str:
    if not entry.get("url_verified") and not allow_unpinned:
        raise SystemExit(
            f"fetch_runtimes: URL not verified for {entry['os']}/{entry['anki']}; "
            "resolve the official asset and set url_verified after review")
    if not entry.get("sha256") and not allow_unpinned:
        raise SystemExit(
            f"fetch_runtimes: no SHA-256 pin for {entry['os']}/{entry['anki']}")
    os.makedirs(dest_dir, exist_ok=True)
    name = entry["url"].rsplit("/", 1)[-1]
    path = os.path.join(dest_dir, name)
    if os.path.isfile(path):
        verify_archive(path, entry, require_pin=not allow_unpinned)
        return path
    print(f"fetch_runtimes: downloading {entry['url']}")
    request = urllib.request.Request(entry["url"], headers={"User-Agent": "ankiscape-ci"})
    with urllib.request.urlopen(request, timeout=120) as resp, \
            open(path, "wb") as fh:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)
    verify_archive(path, entry, require_pin=not allow_unpinned)
    return path


def install(entry: dict, archive: str, dest_dir: str) -> str:
    """Install/mount the pinned runtime for this host and return its binary."""
    kind = entry.get("kind", "")
    if kind == "dmg":
        mount = os.path.join(dest_dir, "mnt")
        os.makedirs(mount, exist_ok=True)
        proc = subprocess.run(
            ["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint",
             mount, archive], capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            raise SystemExit(f"fetch_runtimes: hdiutil attach failed: "
                             f"{(proc.stderr or '')[:200]}")
        binary = os.path.join(mount, "Anki.app", "Contents", "MacOS", "Anki")
        if not os.path.isfile(binary):
            raise SystemExit(f"fetch_runtimes: binary not found in dmg: {binary}")
        return binary
    if kind in ("exe", "msi"):
        if kind == "msi":
            cmd = ["msiexec", "/i", archive, "/qn", "/norestart"]
        else:
            cmd = [archive, "/S"]
        subprocess.run(cmd, timeout=900)
        root = os.environ.get("LOCALAPPDATA", "")
        for candidate in (os.path.join(root, "Programs", "Anki", "anki.exe"),
                          os.path.join(os.environ.get("PROGRAMFILES", ""),
                                       "Anki", "anki.exe")):
            if os.path.isfile(candidate):
                return candidate
        raise SystemExit("fetch_runtimes: installed anki.exe not found")
    if kind in ("tar.zst", "tar.gz", "tar.xz"):
        extract(entry, archive, dest_dir)
        for base, _dirs, files in os.walk(dest_dir):
            if "anki" in files:
                candidate = os.path.join(base, "anki")
                if os.access(candidate, os.X_OK):
                    return candidate
        raise SystemExit("fetch_runtimes: extracted anki binary not found")
    return archive


def extract(entry: dict, archive: str, dest_dir: str) -> str:
    kind = entry.get("kind", "")
    os.makedirs(dest_dir, exist_ok=True)
    if kind in ("tar.zst", "tar.gz", "tar.xz"):
        cmd = ["tar", "-xf", archive, "-C", dest_dir]
    elif kind == "dmg":
        return archive  # mounted by the lane runner
    else:
        return archive  # installer executed by the lane runner
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"fetch_runtimes: extract failed: {exc!r}")
    if proc.returncode != 0:
        raise SystemExit(
            f"fetch_runtimes: extract failed: "
            f"{(proc.stderr or proc.stdout or '')[:200]}")
    for name in sorted(os.listdir(dest_dir)):
        candidate = os.path.join(dest_dir, name, "anki")
        if os.path.isfile(candidate):
            return candidate
    return dest_dir


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="fetch_runtimes.py")
    sub = parser.add_mutually_exclusive_group(required=True)
    sub.add_argument("--list", action="store_true")
    sub.add_argument("--download", metavar="OS/ANKI")
    sub.add_argument("--verify", metavar="ARCHIVE")
    parser.add_argument("--target", default="", help="OS/ANKI for --verify")
    parser.add_argument("--dest", default=os.path.join(ROOT, ".dev", "runtimes"))
    parser.add_argument("--extract", action="store_true")
    parser.add_argument("--install", action="store_true",
                        help="mount/install the runtime and print its binary")
    parser.add_argument("--allow-unpinned", action="store_true")
    args = parser.parse_args(argv)
    manifest = load_manifest()
    if args.list:
        for entry in manifest["targets"]:
            state = ("pinned" if entry.get("sha256") else "UNPINNED")
            url = "verified" if entry.get("url_verified") else "unverified-url"
            print(f"{entry['os']:<8} {entry['anki']:<8} qt{entry['qt']} "
                  f"{entry['kind']:<8} {state} {url}")
        return 0
    if args.verify:
        if not args.target:
            raise SystemExit("fetch_runtimes: --verify needs --target OS/ANKI")
        entry = find_target(manifest, args.target)
        digest = verify_archive(args.verify, entry,
                                require_pin=not args.allow_unpinned)
        print(f"fetch_runtimes: verified {args.target} sha256={digest[:16]}…")
        return 0
    entry = find_target(manifest, args.download)
    path = download(entry, args.dest, allow_unpinned=args.allow_unpinned)
    target_dir = os.path.join(args.dest, entry["os"], entry["anki"])
    if args.install:
        print(install(entry, path, target_dir))
        return 0
    if args.extract:
        path = extract(entry, path, target_dir)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
