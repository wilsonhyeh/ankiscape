# mode.py - Classic/Evolved mode selection (Qt-free, Anki-free core).
"""Requested vs active mode.

- The stored collection key holds the *requested* mode for next start.
- The *active* adapter for a running profile is owned by runtime.py and does
  not change merely because the stored request changes mid-process.
- First profile load presents a chooser with Evolved preselected; closing or
  Escape selects Classic and persists that choice.
"""
from __future__ import annotations

CLASSIC = "classic"
EVOLVED = "evolved"

REQUEST_KEY = "ankiscape_mode_requested"
ACTIVATION_KEY = "ankiscape_evolved_activated_at"

_VALID = {CLASSIC, EVOLVED}


def normalize_requested(value) -> str:
    """Return a valid requested mode; unknown/empty values mean Classic."""
    if isinstance(value, str) and value.strip().lower() in _VALID:
        return value.strip().lower()
    return CLASSIC


def get_requested_mode(get_config) -> str:
    """Read requested mode via a col.get_config-like callable."""
    try:
        raw = get_config(REQUEST_KEY, None)
    except Exception:
        return CLASSIC
    if raw is None:
        return CLASSIC
    return normalize_requested(raw)


def set_requested_mode(set_config, mode: str) -> str:
    """Persist requested mode; returns the normalized value stored."""
    norm = normalize_requested(mode)
    try:
        set_config(REQUEST_KEY, norm)
    except Exception:
        pass
    return norm


def chooser_default() -> str:
    """Evolved is preselected in the first-run chooser."""
    return EVOLVED


def chooser_cancel_result() -> str:
    """Closing/Escape selects Classic and persists that choice."""
    return CLASSIC
