# evolved/icons.py - Deterministic placeholder icons (no runtime downloads).
"""Copy appropriate legacy resource icons; fish/cooked images carry recorded
source/attribution. A deterministic placeholder covers missing assets so a
missing file can never break review or packaging."""
from __future__ import annotations

import base64
import os

# 1x1 transparent PNG (deterministic bytes).
_PLACEHOLDER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def icon_path(*parts: str) -> str:
    return os.path.join(repo_root(), *parts)


def ensure_placeholder(path: str) -> str:
    """Write the deterministic placeholder if missing; return path."""
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(_PLACEHOLDER_PNG)
    return path


def resolve_icon(*, kind: str, display: str, mapping: dict) -> str:
    """Return an existing icon path, else a deterministic placeholder.

    kind: ore|tree|bar|gem|craft|fish|cooked. mapping: legacy display->path.
    """
    _ = kind
    candidate = mapping.get(display, "")
    if candidate and os.path.exists(candidate):
        return candidate
    safe = "".join(ch if ch.isalnum() else "_" for ch in display.strip().lower())
    return ensure_placeholder(icon_path("icon", "placeholder", f"{safe}.png"))


def placeholder_bytes() -> bytes:
    return bytes(_PLACEHOLDER_PNG)
