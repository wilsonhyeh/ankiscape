# evolved/scheduler.py - One cancellable sync scheduler per live game.
"""Schedules sync work around events (login/link, review credit, manual Sync,
leaderboard open) with a first-pending maximum wait of ten seconds. Later
reviews never postpone an armed timer. Only one job runs; requests during a
run coalesce into a single follow-up.

Transient failures (offline, rate limit, no-progress, transient service
errors) get bounded exponential backoff with jitter, honoring Retry-After.
Permanent errors (session expired, ownership mismatch, server upgrade,
protocol failure, rejected progress) pause until the applicable user action
or update instead of polling. Manual retry never bypasses a rate-limit
deadline. Closing a clean profile cancels the timer; nothing polls when idle.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

FIRST_SYNC_MAX_WAIT_S = 10.0
CONTINUATION_DELAY_S = 0.0

# States that should not be retried on a timer: a user action, an update or a
# new event is required. `offline`, `rate_limited`, `no_progress` and
# transient service errors stay retryable.
PERMANENT_STATES = frozenset({
    "session_expired", "game_mismatch", "server_upgrade",
    "rejected_progress", "verification_needed",
})
RETRYABLE_STATES = frozenset({
    "offline", "service_error", "no_progress", "rate_limited", "transient",
})


class SyncScheduler:
    """Driver-agnostic scheduler: the UI supplies `arm` and `run_async`."""

    def __init__(self, service, *, arm: Callable[[float, Callable[[], None]], None],
                 cancel_arm: Callable[[], None],
                 run_async: Callable[[Callable[[], Any], Callable[[Any], None]],
                                     None],
                 now: Callable[[], float] = time.monotonic):
        self.service = service
        self._arm = arm
        self._cancel_arm = cancel_arm
        self._run_async = run_async
        self._now = now
        self._armed = False
        self._running = False
        self._follow_up = False
        self._cancelled = False
        self._rate_limit_until = 0.0
        self._last_reason = ""

    # ------------------------------------------------------------- lifecycle
    def note_pending(self, *, immediate: bool = False) -> None:
        """A new pending operation (or first pending after login). Arms the
        ten-second maximum-wait timer at most once; later calls while armed
        are no-ops (they never postpone it)."""
        if self._cancelled:
            return
        if immediate or self._running:
            if immediate and not self._running:
                self.request(reason="pending")
            elif self._running:
                self._follow_up = True
            return
        if self._armed:
            return
        self._armed = True
        self._arm(FIRST_SYNC_MAX_WAIT_S, self._fire)

    def request(self, *, reason: str = "manual", immediate: bool = True) -> None:
        """Immediate work request: link completion, remembered login, manual
        Sync, leaderboard open. Coalesces if a run is active."""
        if self._cancelled:
            return
        if self._running:
            self._follow_up = True
            return
        delay = 0.0
        wait = self._rate_limit_until - self._now()
        if wait > 0:
            # Manual retry never bypasses a server rate-limit deadline.
            delay = wait
        self._cancel_timer()
        self._armed = True
        self._last_reason = reason
        self._arm(delay, self._fire)

    def cancel(self) -> None:
        self._cancelled = True
        self._follow_up = False
        self._cancel_timer()

    def is_idle(self) -> bool:
        """True when no timer is armed and no run is active: a clean profile
        leaves nothing polling."""
        return not self._armed and not self._running

    # ------------------------------------------------------------- internals
    def _cancel_timer(self) -> None:
        if self._armed:
            try:
                self._cancel_arm()
            except Exception:
                pass
            self._armed = False

    def _fire(self) -> None:
        self._armed = False
        if self._cancelled or self._running:
            return
        self._running = True

        def work() -> Dict[str, Any]:
            return self.service.force_sync()

        def deliver(result: Any) -> None:
            self._running = False
            self._after(result if isinstance(result, dict)
                        else {"ok": False, "status": "service_error"})

        try:
            self._run_async(work, deliver)
        except Exception:
            self._running = False
            self._after({"ok": False, "status": "service_error"})

    def _after(self, result: Dict[str, Any]) -> None:
        if self._cancelled:
            return
        retry_after = result.get("retry_after")
        try:
            retry_after = float(retry_after or 0)
        except (TypeError, ValueError):
            retry_after = 0.0
        if retry_after > 0:
            self._rate_limit_until = max(self._rate_limit_until,
                                         self._now() + retry_after)
        if result.get("ok") and result.get("continue"):
            if self._follow_up or int(result.get("pending", 0) or 0) > 0:
                self._follow_up = False
                self._armed = True
                self._arm(CONTINUATION_DELAY_S, self._fire)
            return
        if self._follow_up:
            self._follow_up = False
            self._armed = True
            self._arm(0.0, self._fire)
            return
        if result.get("ok"):
            return  # clean idle: no polling
        state = str(result.get("status", "") or "")
        if state in RETRYABLE_STATES or result.get("error") in (
                "no_progress", "transient"):
            delay = self.service.backoff_hint_s()
            if self._rate_limit_until > self._now():
                delay = max(delay, self._rate_limit_until - self._now())
            self._armed = True
            self._arm(max(0.0, delay), self._fire)
        # Permanent states: paused until a user action/update/event.
