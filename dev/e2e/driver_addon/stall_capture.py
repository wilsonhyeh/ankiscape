"""Stall capture for the native-performance journey.

Nightly 39656259666 (linux-23.10-qt5, runner image 20261018) showed exactly one
~390 ms main-thread block per add-on rep at a different moment each time, and
the evidence could not say what blocked: faulthandler only dumps after a 90 s
heartbeat stall. This module records, on the lag probe's own clock (`lag_t0`):

- StallWatchdog: a daemon thread that watches the probe's next deadline
  (`state["lag_expected"]`). When the main-thread tick is more than
  `threshold_s` late it captures every thread's Python stack. A call that holds
  the GIL also stops this thread, and so does the whole process being paused
  (macOS runners pause the control run ~150 ms at a time), so its own wake-up
  gaps are recorded too, with a stack snapshot. A gap means "GIL held or
  process paused"; no gap proves nothing, because whether a C call such as
  json.dumps lets other threads run differs by Python build (measured: it
  starved this thread for its full 2.4 s on macOS Python 3.9.6, but for only
  66 ms of 320 ms on the CI runner's Python 3.11).
- TimingHooks: gc runs (every gen-2 run, and gen-0/1 runs at or above
  `min_gc_ms`) via gc.callbacks, and wrapped method timings such as
  Journal.save_checkpoint.

No Qt import: the driver wires it in, tests drive it with a fake clock.
"""
from __future__ import annotations

import gc
import os
import sys
import threading
import time
import traceback

THRESHOLD_S = 0.15
POLL_S = 0.02
MAX_CAPTURES = 20
MAX_FRAMES = 25
MAX_GAPS = 200
MAX_EVENTS = 500


def _thread_names():
    return {t.ident: t.name for t in threading.enumerate()}


def format_stacks(frames=None, names=None, *, skip_ident=None,
                  max_frames=MAX_FRAMES):
    """{thread name: ["file.py:line func", ...]} innermost frame last."""
    frames = sys._current_frames() if frames is None else frames
    names = _thread_names() if names is None else names
    out = {}
    for ident, frame in frames.items():
        if ident == skip_ident:
            continue
        lines = ["%s:%d %s" % (os.path.basename(f.filename), f.lineno, f.name)
                 for f in traceback.extract_stack(frame)[-max_frames:]]
        label = names.get(ident) or "thread-%s" % ident
        if label in out:
            label = "%s-%s" % (label, ident)
        out[label] = lines
    return out


class StallWatchdog:
    """Capture all thread stacks while the main-thread lag tick is late."""

    def __init__(self, state, *, threshold_s=THRESHOLD_S, poll_s=POLL_S,
                 max_captures=MAX_CAPTURES, clock=time.perf_counter,
                 label=None, stacks=None):
        self.state = state
        self.threshold_s = float(threshold_s)
        self.poll_s = float(poll_s)
        self.max_captures = int(max_captures)
        self.clock = clock
        self.label = label or (lambda: "")
        self.stacks = stacks or (lambda: format_stacks(
            skip_ident=threading.get_ident()))
        self.captures = []
        self.gaps = []
        self.missed = 0
        self._captured_for = None
        self._last_check = None
        self._stop = threading.Event()
        self._thread = None

    def check(self, now=None):
        """One watchdog step; returns the capture it recorded, if any."""
        now = self.clock() if now is None else now
        gap = None if self._last_check is None else now - self._last_check
        self._last_check = now
        t0 = self.state.get("lag_t0", now)
        if gap is not None and gap - self.poll_s > self.threshold_s / 2:
            # This thread could not run either: something held the GIL or
            # the process was descheduled for that long. The main thread
            # usually re-arms its deadline before this thread runs again, so
            # no late-tick capture follows; snapshot the stacks here instead.
            # Taken just after the release, the holder is typically still in
            # its caller (measured on Python 3.9.6, Anki's 3.9 line: a 2.4 s
            # json.dumps in a worker thread showed up only as this gap).
            if len(self.gaps) < MAX_GAPS:
                entry = {"at": round(now - t0, 3),
                         "gap_ms": round(gap * 1000.0, 1),
                         "phase": self._safe_label()}
                if sum(1 for g in self.gaps if "threads" in g) \
                        < self.max_captures:
                    try:
                        entry["threads"] = self.stacks()
                    except Exception as exc:  # noqa: BLE001
                        entry["threads"] = {"error": [repr(exc)[:200]]}
                self.gaps.append(entry)
        expected = self.state.get("lag_expected")
        if expected is None or expected == self._captured_for:
            return None
        late = now - expected
        if late <= self.threshold_s:
            return None
        self._captured_for = expected
        if len(self.captures) >= self.max_captures:
            self.missed += 1
            return None
        try:
            stacks = self.stacks()
        except Exception as exc:  # noqa: BLE001 - evidence, never crash Anki
            stacks = {"error": [repr(exc)[:200]]}
        capture = {"at": round(now - t0, 3),
                   "late_ms": round(late * 1000.0, 1),
                   "watchdog_gap_ms": (round(gap * 1000.0, 1)
                                       if gap is not None else None),
                   "phase": self._safe_label(),
                   "threads": stacks}
        self.captures.append(capture)
        return capture

    def _safe_label(self):
        try:
            return str(self.label())
        except Exception:
            return ""

    def _run(self):
        while not self._stop.is_set():
            if self.state.get("lag_probe_off"):
                break
            try:
                self.check()
            except Exception:
                pass
            self._stop.wait(self.poll_s)

    def start(self):
        self._thread = threading.Thread(target=self._run,
                                        name="ankiscape-stall-watchdog",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=1.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def report(self):
        return {"threshold_ms": round(self.threshold_s * 1000.0, 1),
                "captures": list(self.captures),
                "missed_captures": self.missed,
                "watchdog_gaps": list(self.gaps)}


class TimingHooks:
    """gc runs and wrapped-call timings on the lag probe's clock."""

    def __init__(self, state, *, clock=time.perf_counter, min_gc_ms=5.0,
                 max_events=MAX_EVENTS, label=None):
        self.state = state
        self.clock = clock
        self.min_gc_ms = float(min_gc_ms)
        self.max_events = int(max_events)
        self.label = label or (lambda: "")
        self.gc_events = []
        self.gc_counts = {}
        self.gc_dropped = 0
        self.calls = {}
        self._gc_start = None
        self._patched = []

    def _at(self, now):
        return round(now - self.state.get("lag_t0", now), 3)

    def gc_callback(self, phase, info):
        now = self.clock()
        if phase == "start":
            self._gc_start = now
            return
        start, self._gc_start = self._gc_start, None
        if start is None:
            return
        gen = int((info or {}).get("generation", -1))
        ms = (now - start) * 1000.0
        key = str(gen)
        entry = self.gc_counts.setdefault(key, {"runs": 0, "total_ms": 0.0,
                                                "max_ms": 0.0})
        entry["runs"] += 1
        entry["total_ms"] = round(entry["total_ms"] + ms, 3)
        entry["max_ms"] = round(max(entry["max_ms"], ms), 3)
        if gen == 2 or ms >= self.min_gc_ms:
            if len(self.gc_events) >= self.max_events:
                self.gc_dropped += 1
                return
            self.gc_events.append({
                "at": self._at(start), "ms": round(ms, 3), "generation": gen,
                "collected": (info or {}).get("collected"),
                "thread": threading.current_thread().name,
                "phase": self._safe_label()})

    def install_gc(self):
        if self.gc_callback not in gc.callbacks:
            gc.callbacks.append(self.gc_callback)

    def uninstall_gc(self):
        try:
            gc.callbacks.remove(self.gc_callback)
        except ValueError:
            pass

    def wrap(self, owner, attr, kind):
        """Time every call of owner.attr; returns False when it is missing."""
        original = getattr(owner, attr, None)
        if original is None or getattr(original, "_stall_capture_kind", None):
            return False
        hooks = self

        def timed(*args, **kwargs):
            start = hooks.clock()
            try:
                return original(*args, **kwargs)
            finally:
                hooks._record_call(kind, start, hooks.clock())

        timed._stall_capture_kind = kind
        timed.__wrapped__ = original
        setattr(owner, attr, timed)
        self._patched.append((owner, attr, original))
        self.calls.setdefault(kind, [])
        return True

    def _record_call(self, kind, start, end):
        events = self.calls.setdefault(kind, [])
        if len(events) >= self.max_events:
            return
        events.append({"at": self._at(start),
                       "ms": round((end - start) * 1000.0, 3),
                       "thread": threading.current_thread().name,
                       "phase": self._safe_label()})

    def restore(self):
        for owner, attr, original in reversed(self._patched):
            try:
                setattr(owner, attr, original)
            except Exception:
                pass
        self._patched = []
        self.uninstall_gc()

    def _safe_label(self):
        try:
            return str(self.label())
        except Exception:
            return ""

    def report(self):
        return {"gc_events": list(self.gc_events),
                "gc_counts": dict(self.gc_counts),
                "gc_events_dropped": self.gc_dropped,
                "calls": {k: list(v) for k, v in self.calls.items()}}
