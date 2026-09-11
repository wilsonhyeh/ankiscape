# evolved/ui/stale.py - Pure stale-response guard (no Qt).
"""Small pure predicate used by screens that accept asynchronous results.
Kept Qt-free so the guard itself is unit-testable and mutation-checked."""
from __future__ import annotations


def response_is_stale(*, closed: bool, request_id: int,
                      current_request_id: int) -> bool:
    """True when a completed callback must be discarded: the screen is
    closed, or a newer request (skill switch, refresh, cohort toggle or
    profile change) superseded it. Never render a stale result."""
    return bool(closed) or int(request_id) != int(current_request_id)
