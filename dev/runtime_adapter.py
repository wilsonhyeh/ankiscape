#!/usr/bin/env python3
# dev/runtime_adapter.py - Native Anki runtime resolution and ownership.
"""Platform-specific executable resolution, version probing, owned-process
launch/termination, profile locking, screenshots and path normalization for
the native matrix. Supports macOS app bundles, Windows executables and Linux
binaries; an explicit --anki-bin always wins. Only processes this repo
launched are terminated: no blanket pkill, no personal profile access, and
single-instance forwarding is treated as a failure."""
from __future__ import annotations

import os
import platform
import json
import plistlib
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNED_MARKER_ENV = "ANKISCAPE_OWNED_RUN"


def normalize_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def host_os() -> str:
    name = platform.system().lower()
    return {"darwin": "macos", "windows": "win", "linux": "linux"}.get(name, name)


def version_matches(requested: str, have: str) -> bool:
    def _norm(value):
        return tuple(int(p) if p.isdigit() else p for p in str(value).split("."))
    if not requested:
        return True
    want, got = _norm(requested), _norm(have)
    return len(got) >= len(want) and got[:len(want)] == want


@dataclass
class RuntimeInfo:
    binary: str
    version: str           # observed when probed, else the requested string
    version_source: str    # "probe" | "requested" | "bundle" | "path"
    app: str = ""

    def as_record(self) -> dict:
        return {"binary": self.binary, "version": self.version,
                "version_source": self.version_source, "app": self.app}


def _probe_version(binary: str) -> str:
    """Best-effort runtime version probe. Returns "" when unprobeable."""
    for args in (["--version"], ["-V"]):
        try:
            proc = subprocess.run([binary, *args], capture_output=True,
                                  text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        text = f"{proc.stdout}\n{proc.stderr}"
        match = re.search(r"\b(\d{2,4}\.\d{1,2}(?:\.\d{1,2})?)\b", text)
        if proc.returncode == 0 and match:
            return match.group(1)
    return _probe_version_from_files(binary)


def _probe_version_from_files(binary: str) -> str:
    """Version from the release layout, no process launch (works headless):
    macOS bundle Info.plist, Linux anki-<v>.dist-info, or a version file."""
    base = os.path.dirname(os.path.abspath(binary))
    plist = os.path.join(base, "..", "Info.plist")
    if os.path.isfile(plist):
        try:
            with open(plist, "rb") as fh:
                version = plistlib.load(fh).get("CFBundleShortVersionString", "")
            if version:
                return str(version)
        except OSError:
            pass
    for folder in (base, os.path.join(base, "app")):
        stamp = os.path.join(folder, "runtime-version.json")
        if os.path.isfile(stamp):
            try:
                with open(stamp, encoding="utf-8") as fh:
                    value = str((json.load(fh) or {}).get("anki", ""))
                if re.match(r"^\d+(?:\.\d+)+$", value):
                    return value
            except (OSError, ValueError):
                pass
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            match = re.match(r"anki-(\d+(?:\.\d+)+)\.dist-info$", name)
            if match:
                return match.group(1)
        for filename in ("version", "VERSION"):
            candidate = os.path.join(folder, filename)
            if os.path.isfile(candidate):
                try:
                    with open(candidate, encoding="utf-8") as fh:
                        value = fh.read().strip()
                    if re.match(r"^\d+(?:\.\d+)+$", value):
                        return value
                except OSError:
                    pass
    return ""


def _macos_candidates(requested: str):
    candidates = []
    apps = "/Applications"
    if os.path.isdir(apps):
        for name in sorted(os.listdir(apps)):
            if name == "Anki.app" or (name.startswith("Anki ")
                                      and name.endswith(".app")):
                candidates.append(os.path.join(apps, name))
    for app in candidates:
        exe = os.path.join(app, "Contents", "MacOS", "Anki")
        if not os.path.isfile(exe):
            continue
        version = ""
        try:
            with open(os.path.join(app, "Contents", "Info.plist"), "rb") as fh:
                version = plistlib.load(fh).get("CFBundleShortVersionString", "")
        except OSError:
            version = ""
        if version_matches(requested, version):
            return RuntimeInfo(binary=exe, version=version or requested,
                               version_source="bundle", app=app)
    return None


def _windows_candidates(requested: str):
    roots = [os.environ.get("LOCALAPPDATA", ""), os.environ.get("PROGRAMFILES", ""),
             os.environ.get("PROGRAMFILES(X86)", "")]
    for root in roots:
        if not root:
            continue
        for candidate in (os.path.join(root, "Programs", "Anki", "anki.exe"),
                          os.path.join(root, "Anki", "anki.exe")):
            if os.path.isfile(candidate):
                return RuntimeInfo(binary=candidate, version=requested,
                                   version_source="path")
    return None


def _linux_candidates(requested: str):
    for candidate in ("/usr/local/bin/anki", "/usr/bin/anki",
                      os.path.join(ROOT, ".dev", "anki", "anki")):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            version = _probe_version(candidate)
            if version_matches(requested, version):
                return RuntimeInfo(binary=candidate, version=version or requested,
                                   version_source="probe" if version else "path")
    return None


def resolve_anki(requested: str = "", binary: str = "",
                 declared_version: str = "") -> Optional[RuntimeInfo]:
    """Resolve the runtime for this host. An explicit binary wins; its
    version comes from probing (process, then release layout). A declared
    version is only accepted together with an explicit binary and is
    recorded as 'declared' so evidence never implies a probe that did not
    happen (the hash-verified download is the real version guarantee)."""
    if binary:
        if not os.path.isfile(binary):
            return None
        version = _probe_version(binary)
        if version:
            return RuntimeInfo(binary=binary, version=version,
                               version_source="probe")
        if declared_version:
            return RuntimeInfo(binary=binary, version=declared_version,
                               version_source="declared")
        return RuntimeInfo(binary=binary, version=requested,
                           version_source="requested")
    if host_os() == "macos":
        return _macos_candidates(requested)
    if host_os() == "win":
        return _windows_candidates(requested)
    return _linux_candidates(requested)


def verify_runtime(info: Optional[RuntimeInfo], requested: str) -> Tuple[bool, str]:
    """Actual runtime version must match the requested target before testing."""
    if info is None:
        return False, "no runtime resolved"
    if info.version_source == "requested":
        return False, ("runtime version unprobeable; pass an explicit "
                       "--anki-actual or use a probeable binary")
    if not version_matches(requested, info.version):
        return False, f"runtime {info.version} != requested {requested}"
    return True, info.version


def running_anki_processes() -> list:
    """PIDs of any Anki process on this host (never killed; used to refuse)."""
    try:
        if host_os() == "win":
            proc = subprocess.run(["tasklist", "/FI", "IMAGENAME eq anki.exe"],
                                  capture_output=True, text=True, timeout=15)
            return [line.split()[1] for line in (proc.stdout or "").splitlines()
                    if line.lower().startswith("anki.exe")]
        flag = "-x" if host_os() == "macos" else "-f"
        pattern = "Anki" if host_os() == "macos" else "anki"
        proc = subprocess.run(["pgrep", flag, pattern], capture_output=True,
                              text=True, timeout=15)
        return [line.strip() for line in (proc.stdout or "").splitlines()
                if line.strip()]
    except (OSError, subprocess.SubprocessError):
        return []


def refuse_forwarding(context: str) -> Optional[str]:
    """A running Anki would swallow -b/-p via single-instance forwarding and
    open the PERSONAL profile instead. Refuse outright (never kill it)."""
    pids = running_anki_processes()
    if pids:
        return (f"an Anki process is already running ({context}, pids={pids[:4]}); "
                "close it first — the native matrix never drives a foreign process")
    return None


def profile_locked(base: str, profile: str) -> Optional[str]:
    """Detect this base/profile being held (leftover or forwarded instance)."""
    marker = os.path.join(base, "collection.anki2-journal")
    if os.path.exists(marker):
        return f"profile lock present: {marker}"
    try:
        if host_os() == "win":
            return None
        proc = subprocess.run(["pgrep", "-af", "Anki"], capture_output=True,
                              text=True, timeout=15)
        if normalize_path(base) in (proc.stdout or ""):
            return "an Anki process references this base"
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def launch_owned(info: RuntimeInfo, base: str, profile: str, env: dict,
                 log_path: str):
    """Launch the runtime with an ownership marker and returned Popen."""
    child_env = dict(env or {})
    child_env[OWNED_MARKER_ENV] = "1"
    log = open(log_path, "a", encoding="utf-8")
    try:
        return subprocess.Popen([info.binary, "-b", base, "-p", profile],
                                stdout=log, stderr=subprocess.STDOUT,
                                env=child_env), log
    except OSError:
        log.close()
        raise


def kill_owned(child, *, base: str = "", timeout: float = 10.0) -> bool:
    """Stop only the process this run launched (escalate, then verify)."""
    if child is None:
        return True
    try:
        child.terminate()
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        if child.poll() is None:
            child.kill()
            try:
                child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return False
        return True
    except OSError:
        return False
    finally:
        reap_audio_helpers(base)


def reap_audio_helpers(base: str) -> None:
    """Kill only mpv helpers that reference OUR base (macOS/Linux)."""
    if host_os() == "win" or not base:
        return
    try:
        proc = subprocess.run(["pgrep", "-af", "anki_audio/mpv"],
                              capture_output=True, text=True, timeout=10)
        for line in (proc.stdout or "").splitlines():
            if base in line:
                try:
                    os.kill(int(line.split()[0]), 9)
                except (ValueError, OSError):
                    pass
    except (OSError, subprocess.SubprocessError):
        pass


def requires_desktop() -> Optional[str]:
    """Refuse a Qt-offscreen environment: it is not a packaged native run."""
    if os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen":
        return "QT_QPA_PLATFORM=offscreen is not a native desktop session"
    return None


def screenshot(mw, path: str) -> bool:
    try:
        return bool(mw.grab().save(path))
    except Exception:
        return False


def wait_for_exit(child, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            return True
        time.sleep(0.25)
    return False


def ensure_display(env: dict) -> dict:
    """Linux CI: use Xvfb when no display is present."""
    out = dict(env or {})
    if host_os() == "linux" and not out.get("DISPLAY"):
        out.setdefault("DISPLAY", ":99")
    return out
