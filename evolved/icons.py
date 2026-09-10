# evolved/icons.py - Shipped fallback icon resolution (no runtime downloads).
"""Every unknown item resolves to ONE shipped, visible fallback tile.

Nothing is written to the installed add-on at runtime: a missing file can
never break review or packaging, and a missing image never creates a new
file inside the installation. Known supported items are audited
(`scripts/audit_assets.py`) to never resolve through this fallback; the
deterministic bytes below exist only so tests and tools can recognize it.
"""
from __future__ import annotations

import os

FALLBACK_REL = os.path.join("icon", "fallback_missing.png")


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def icon_path(*parts: str) -> str:
    return os.path.join(repo_root(), *parts)


def fallback_path() -> str:
    """Absolute path of the single bundled fallback image."""
    return icon_path(FALLBACK_REL)


def resolve_icon(*, kind: str, display: str, mapping: dict) -> str:
    """Return an existing icon path, else the shipped fallback.

    kind/display are informational only; resolution never creates a file.
    """
    _ = kind, display
    candidate = mapping.get(display, "")
    if candidate and os.path.exists(candidate):
        return candidate
    return fallback_path()


def placeholder_bytes() -> bytes:
    """The shipped fallback's bytes (deterministic; read-only)."""
    with open(fallback_path(), "rb") as fh:
        return fh.read()
