# evolved/diagnostics.py - Allowlisted, private-by-default diagnostics (pure).
"""Diagnostics are structured allowlisted fields, never regex-cleaned raw log
dumps. The collector can only emit fields from FIELD_ALLOWLIST, so passwords,
tokens, cookies, headers, emails, usernames, account/game/device ids,
collection/deck/card/note content, revlog data, host/user/computer names,
full paths, IPs, arbitrary exception text and backend response bodies cannot
appear in a report even if a caller passes them.

A bounded ring (<=64 KiB) keeps recent structured diagnostics locally. The
ring, rotation and rendering never affect reviewing."""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Tuple

ISSUE_BASE = "https://github.com/wilsonhyeh/ankiscape/issues/new"
URL_MAX_BYTES = 1800
RING_MAX_BYTES = 64 * 1024
TEMPLATE_BUG = "bug_report.yml"

PENDING_BUCKETS = ((0, "0"), (4, "1-4"), (19, "5-19"), (99, "20-99"),
                   (10 ** 9, "100+"))
TOGGLE_ALLOWLIST = ("hud_visible", "celebrations", "reduced_motion", "sound",
                    "hud_position")
TIMING_ALLOWLIST = ("accepted_answer_hook_p95", "reward_completion_p95",
                    "rebuild100k_ms", "event_loop_lag_p95", "queue_latency_p95")
MODE_VALUES = ("classic", "evolved")


def _bucket(value: int) -> str:
    try:
        count = max(0, int(value))
    except (TypeError, ValueError):
        count = 0
    for limit, label in PENDING_BUCKETS:
        if count <= limit:
            return label
    return "100+"


def _timing_bucket(ms: Any) -> str:
    try:
        value = float(ms)
    except (TypeError, ValueError):
        return "unknown"
    for limit, label in ((20, "<=20ms"), (50, "<=50ms"), (100, "<=100ms"),
                         (250, "<=250ms"), (1000, "<=1s"), (8000, "<=8s")):
        if value <= limit:
            return label
    return ">8s"


def _safe_version(value: Any) -> str:
    text = str(value or "").strip()
    return re.sub(r"[^0-9A-Za-z._+\- ]", "", text)[:40]


def _safe_code(value: Any) -> str:
    text = str(value or "").strip().lower().replace(" ", "_")
    return re.sub(r"[^a-z0-9_.:\-]", "", text)[:60]


def collect(*, addon_version: str = "", artifact_id: str = "",
            anki: str = "", python: str = "", qt: str = "", os_name: str = "",
            arch: str = "", mode: str = "", error_code: str = "",
            pending: int = 0, toggles: Optional[Dict[str, Any]] = None,
            timings: Optional[Dict[str, Any]] = None, recovery: bool = False,
            worker_failed: bool = False, logged_in: bool = False) -> Dict[str, Any]:
    """Build the exact payload a report may carry. Every field is allowlisted
    and coarsened; unexpected input cannot leak through."""
    payload: Dict[str, Any] = {
        "schema": "ankiscape-report-v1",
        "addon_version": _safe_version(addon_version),
        "artifact_id": _safe_version(artifact_id),
        "anki": _safe_version(anki),
        "python": _safe_version(python),
        "qt": _safe_version(qt),
        "os": _safe_version(os_name),
        "arch": _safe_version(arch),
        "mode": mode if mode in MODE_VALUES else "unknown",
        "error_code": _safe_code(error_code),
        "pending_bucket": _bucket(pending),
        "recovery": bool(recovery),
        "worker_failed": bool(worker_failed),
        "logged_in": bool(logged_in),
        "toggles": {},
        "timings": {},
    }
    for key in TOGGLE_ALLOWLIST:
        if toggles and key in toggles:
            payload["toggles"][key] = bool(toggles[key])
    for key in TIMING_ALLOWLIST:
        if timings and key in timings:
            payload["timings"][key] = _timing_bucket(timings[key])
    return payload


def render_text(payload: Dict[str, Any]) -> str:
    """Deterministic human-readable rendering; exactly what previews/copies."""
    payload = payload or {}
    lines = [f"addon: {payload.get('addon_version', '?')}"
             f" (artifact {payload.get('artifact_id', '?')})",
             f"anki: {payload.get('anki', '?')} | python: "
             f"{payload.get('python', '?')} | qt: {payload.get('qt', '?')}",
             f"os: {payload.get('os', '?')} {payload.get('arch', '?')}",
             f"mode: {payload.get('mode', '?')}",
             f"error_code: {payload.get('error_code', '') or 'none'}",
             f"pending: {payload.get('pending_bucket', '0')}",
             f"recovery: {bool(payload.get('recovery'))} | "
             f"worker_failed: {bool(payload.get('worker_failed'))} | "
             f"logged_in: {bool(payload.get('logged_in'))}"]
    toggles = payload.get("toggles") or {}
    if toggles:
        lines.append("toggles: " + ", ".join(
            f"{k}={'on' if v else 'off'}" for k, v in sorted(toggles.items())))
    timings = payload.get("timings") or {}
    if timings:
        lines.append("timings: " + ", ".join(
            f"{k}={v}" for k, v in sorted(timings.items())))
    return "\n".join(lines)


def report_body(summary: str, happened: str, expected: str, steps: str,
                diagnostics_text: str = "") -> str:
    parts = ["## Summary", summary.strip() or "(describe the problem)",
             "", "## What happened", happened.strip() or "(what you saw)",
             "", "## What you expected", expected.strip() or "(expected)",
             "", "## Steps to reproduce", steps.strip() or "(1. ...)",
             "", "## Reproducibility", "(always / sometimes / once)"]
    if diagnostics_text:
        parts += ["", "## Diagnostics (allowlisted)", "```",
                  diagnostics_text.strip(), "```"]
    return "\n".join(parts)


def issue_url(*, title: str, body: str, template: str = TEMPLATE_BUG,
              base: str = ISSUE_BASE) -> Tuple[Optional[str], str]:
    """Prefilled issue URL. Returns (url, body): when the encoded URL would
    exceed URL_MAX_BYTES the caller must copy the body and open the blank
    template instead (url=None)."""
    query = urllib.parse.urlencode({
        "template": template, "title": title[:120], "body": body})
    url = f"{base}?{query}"
    if len(url.encode("utf-8")) > URL_MAX_BYTES:
        blank = f"{base}?{urllib.parse.urlencode({'template': template})}"
        return None, body
    return url, body


def normalize_traceback(text: Any) -> List[str]:
    """Keep only shipped module/function/line frames; drop locals, message
    text and paths. Used only for maintainer-side context, never for secrets."""
    frames: List[str] = []
    pattern = re.compile(
        r'File "[^"]*(ankiscape[/\\][^"]+)", line (\d+), in ([A-Za-z0-9_<>]+)')
    for match in pattern.finditer(str(text or "")):
        path = match.group(1).replace("\\", "/")
        module = path[:-3].replace("/", ".")[:120] if path.endswith(".py") \
            else path[:120]
        frames.append(f"{module}:{match.group(2)}:{match.group(3)}")
    return frames[-20:]


class DiagnosticRing:
    """Bounded local ring (<=64 KiB serialized). Never raises into review."""

    def __init__(self, max_bytes: int = RING_MAX_BYTES):
        self.max_bytes = int(max_bytes)
        self._entries: List[Dict[str, Any]] = []

    def append(self, payload: Dict[str, Any]) -> None:
        try:
            entry = dict(payload or {})
            entry.setdefault("at", int(time.time()))
            self._entries.append(entry)
            self._trim()
        except Exception:
            pass

    def _trim(self) -> None:
        while len(self._entries) > 1 and self.size_bytes() > self.max_bytes:
            self._entries.pop(0)

    def size_bytes(self) -> int:
        return len(self.serialized().encode("utf-8"))

    def serialized(self) -> str:
        try:
            return json.dumps(self._entries, sort_keys=True, ensure_ascii=False)
        except Exception:
            return "[]"

    def entries(self) -> List[Dict[str, Any]]:
        return list(self._entries)

    def clear(self) -> None:
        self._entries = []


_RING = DiagnosticRing()


def record(payload: Dict[str, Any]) -> None:
    _RING.append(payload)


def recent(limit: int = 20) -> List[Dict[str, Any]]:
    return _RING.entries()[-max(1, limit):]


def ring_size_bytes() -> int:
    return _RING.size_bytes()
