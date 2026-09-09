# evolved/ui/hud.py - Evolved HUD owner (one live owner per profile).
"""Frameless, never steals keyboard focus, hidden outside review and on
profile close. One coalesced pending update per component (no repeated
timers, no idle polling). Catch-up produces one summary, never popups per
card. Merge/Undo adjustments show a concise explanation, nonmodally.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


@dataclass
class HudState:
    visible: bool = False
    skill: str = "mining"
    level: int = 1
    progress: float = 0.0
    total_level: int = 6
    pending_update: bool = False
    last_paint_ms: Optional[float] = None
    adjustments: str = ""


class HudOwner:
    """Qt-free state machine; the Qt widget (when present) binds to this."""

    def __init__(self, apply: Optional[Callable[[HudState], None]] = None):
        self.state = HudState()
        self._apply = apply
        self._paint_samples: list = []

    def request_update(self, **fields) -> None:
        for key, value in fields.items():
            if hasattr(self.state, key):
                setattr(self.state, key, value)
        # Coalesce: mark pending; the single queued pump applies it.
        self.state.pending_update = True

    def pump(self) -> bool:
        """Apply one coalesced update. Returns True if a paint happened."""
        if not self.state.pending_update:
            return False
        self.state.pending_update = False
        started = time.perf_counter()
        if self._apply is not None:
            try:
                self._apply(self.state)
            except Exception:
                pass
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.state.last_paint_ms = elapsed_ms
        self._paint_samples.append(elapsed_ms)
        return True

    def hide_on_leave_review(self) -> None:
        self.state.visible = False
        self.state.pending_update = True

    def release(self) -> None:
        """Profile close: hide, drop pending work, release widget binding."""
        self.state.visible = False
        self.state.pending_update = False
        self.state.adjustments = ""
        self._apply = None

    def p95_paint_ms(self) -> Optional[float]:
        if not self._paint_samples:
            return None
        ordered = sorted(self._paint_samples)
        idx = min(len(ordered) - 1, int(len(ordered) * 0.95))
        return ordered[idx]
