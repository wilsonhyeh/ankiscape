import unittest

from evolved.scheduler import FIRST_SYNC_MAX_WAIT_S, SyncScheduler


class _Service:
    def __init__(self, results=None, backoff=2.0):
        self.results = list(results or [{"ok": True, "status": "ok",
                                         "pending": 0}])
        self.calls = 0
        self.backoff = backoff

    def force_sync(self):
        self.calls += 1
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]

    def backoff_hint_s(self):
        return self.backoff


class _Sched:
    """Capture armed timers and run them on demand."""

    def __init__(self, service, *, sync_immediately=True):
        self.armed = []
        self.held = []
        self.cancelled = 0
        self.service = service
        self.sync_immediately = sync_immediately
        self.scheduler = SyncScheduler(
            service, arm=self._arm, cancel_arm=self._cancel,
            run_async=self._run_async, now=lambda: 0.0)

    def _arm(self, delay, callback):
        self.armed.append([float(delay), callback])

    def _cancel(self):
        self.cancelled += 1

    def _run_async(self, work, deliver):
        if self.sync_immediately:
            deliver(work())
        else:
            self.held.append((work, deliver))

    def deliver_next(self):
        work, deliver = self.held.pop(0)
        deliver(work())

    def fire(self, index=-1):
        delay, callback = self.armed.pop(index)
        callback()
        return delay


class TestScheduler(unittest.TestCase):
    def test_first_pending_arms_ten_seconds_once(self):
        svc = _Service()
        s = _Sched(svc)
        s.scheduler.note_pending()
        self.assertEqual(len(s.armed), 1)
        self.assertEqual(s.armed[0][0], FIRST_SYNC_MAX_WAIT_S)
        s.scheduler.note_pending()
        s.scheduler.note_pending()
        self.assertEqual(len(s.armed), 1, "later reviews must not postpone")

    def test_request_arms_zero_delay_and_runs_once(self):
        svc = _Service()
        s = _Sched(svc)
        s.scheduler.request(reason="manual")
        self.assertEqual(s.armed[-1][0], 0.0)
        s.fire()
        self.assertEqual(svc.calls, 1)

    def test_requests_during_run_coalesce_into_one_follow_up(self):
        svc = _Service([{"ok": True, "status": "ok", "pending": 0},
                        {"ok": True, "status": "ok", "pending": 0}])
        s = _Sched(svc, sync_immediately=False)
        s.scheduler.request(reason="manual")
        delay, callback = s.armed.pop(0)
        callback()  # starts the run; delivery is held
        s.scheduler.request(reason="review")
        s.scheduler.request(reason="leaderboard")
        self.assertEqual(svc.calls, 0, "work runs off the UI thread")
        s.deliver_next()  # first run completes
        self.assertEqual(svc.calls, 1)
        self.assertEqual(s.armed[-1][0], 0.0, "one coalesced follow-up")
        delay, callback = s.armed.pop(0)
        callback()
        s.deliver_next()
        self.assertEqual(svc.calls, 2)

    def test_transient_failure_backs_off_with_jitter_bound(self):
        svc = _Service([{"ok": False, "status": "offline"}])
        s = _Sched(svc)
        s.scheduler.request(reason="manual")
        s.fire()
        self.assertEqual(len(s.armed), 1)
        self.assertEqual(s.armed[0][0], svc.backoff)

    def test_permanent_state_pauses_without_timer(self):
        svc = _Service([{"ok": False, "status": "session_expired"}])
        s = _Sched(svc)
        s.scheduler.request(reason="manual")
        s.fire()
        self.assertEqual(s.armed, [])

    def test_rate_limit_deadline_gates_manual_retry(self):
        now = {"t": 0.0}
        svc = _Service([{"ok": False, "status": "rate_limited",
                         "retry_after": 30}, {"ok": True, "status": "ok"}])
        s = _Sched(svc)
        s.scheduler._now = lambda: now["t"]
        s.scheduler.request(reason="manual")
        s.fire()
        # The run returned rate_limited; a manual retry must wait out the
        # deadline rather than bypassing it.
        s.scheduler.request(reason="manual")
        self.assertEqual(s.armed[-1][0], 30.0)

    def test_cancel_is_idle_and_no_polling(self):
        svc = _Service()
        s = _Sched(svc)
        s.scheduler.note_pending()
        s.scheduler.cancel()
        self.assertTrue(s.scheduler.is_idle())
        self.assertEqual(s.cancelled, 1)
        s.scheduler.note_pending()
        self.assertEqual(len(s.armed), 1, "a cancelled scheduler arms nothing new")

    def test_successful_pass_leaves_no_timer(self):
        svc = _Service([{"ok": True, "status": "ok", "pending": 0}])
        s = _Sched(svc)
        s.scheduler.request(reason="manual")
        s.fire()
        self.assertTrue(s.scheduler.is_idle())

    def test_continuation_reschedules_when_pending_remains(self):
        svc = _Service([{"ok": True, "status": "continue", "continue": True,
                         "pending": 3}])
        s = _Sched(svc)
        s.scheduler.request(reason="manual")
        s.fire()
        self.assertEqual(s.armed[-1][0], 0.0)


if __name__ == "__main__":
    unittest.main()
