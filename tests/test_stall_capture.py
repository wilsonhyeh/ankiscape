# tests/test_stall_capture.py - the perf driver names what blocked a stall.
"""Nightly 39656259666 (linux-23.10-qt5) had one ~390 ms main-thread block per
add-on rep and no evidence of what blocked. The perf driver now runs a watchdog
that captures every thread's stack when the lag tick is >150 ms late, records
its own wake-up gaps (a held GIL starves it too), and times gc runs and
checkpoint work on the lag probe's clock. native_performance turns that into one
note per stall."""
from __future__ import annotations

import gc
import importlib.util
import os
import sys
import threading
import time
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRIVER_DIR = os.path.join(ROOT, "dev", "e2e", "driver_addon")


def _load(name: str, path: str, package_dir: str = ""):
    kwargs = {"submodule_search_locations": [package_dir]} if package_dir else {}
    spec = importlib.util.spec_from_file_location(name, path, **kwargs)
    module = importlib.util.module_from_spec(spec)
    if package_dir:
        sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SC = _load("ankiscape_stall_capture_under_test",
           os.path.join(DRIVER_DIR, "stall_capture.py"))
NP = _load("ankiscape_native_performance_stall_notes",
           os.path.join(ROOT, "dev", "native_performance.py"))


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


class WatchdogTests(unittest.TestCase):
    def make(self, **kwargs):
        self.clock = Clock()
        self.state = {"lag_t0": 90.0, "lag_expected": 100.1}
        self.calls = []

        def stacks():
            self.calls.append(self.clock.now)
            return {"MainThread": ["journal.py:564 save_checkpoint"]}

        return SC.StallWatchdog(self.state, clock=self.clock, stacks=stacks,
                                label=lambda: "answers:84", **kwargs)

    def test_on_time_tick_captures_nothing(self):
        wd = self.make()
        for now in (100.02, 100.08, 100.2):
            self.assertIsNone(wd.check(now))
        self.assertEqual(wd.captures, [])

    def test_late_tick_captures_all_stacks_once_per_deadline(self):
        wd = self.make()
        wd.check(100.2)
        capture = wd.check(100.27)          # 170 ms past the 100.1 deadline
        self.assertIsNotNone(capture)
        self.assertEqual(capture["at"], 10.27)
        self.assertEqual(capture["late_ms"], 170.0)
        self.assertEqual(capture["phase"], "answers:84")
        self.assertEqual(capture["threads"]["MainThread"],
                         ["journal.py:564 save_checkpoint"])
        self.assertIsNone(wd.check(100.40))  # same stall, same deadline
        self.assertEqual(len(wd.captures), 1)
        self.state["lag_expected"] = 100.6   # the tick fired and re-armed
        self.assertIsNone(wd.check(100.62))
        self.assertIsNotNone(wd.check(100.80))
        self.assertEqual(len(wd.captures), 2)

    def test_capture_count_is_bounded(self):
        wd = self.make(max_captures=1)
        wd.check(100.3)
        self.state["lag_expected"] = 101.0
        wd.check(101.3)
        self.assertEqual(len(wd.captures), 1)
        self.assertEqual(wd.report()["missed_captures"], 1)

    def test_a_starved_watchdog_records_its_own_gap(self):
        wd = self.make()
        wd.check(100.00)
        wd.check(100.02)                     # normal 20 ms poll
        capture = wd.check(100.42)           # woke 400 ms later: GIL held
        self.assertEqual(capture["watchdog_gap_ms"], 400.0)
        self.assertEqual(wd.gaps, [{"at": 10.42, "gap_ms": 400.0,
                                    "phase": "answers:84", "threads": {
                                        "MainThread":
                                            ["journal.py:564 save_checkpoint"]}}])
        self.assertEqual(len(wd.report()["watchdog_gaps"]), 1)

    def test_gap_snapshot_needs_no_late_tick(self):
        # The main thread re-armed its deadline before the watchdog woke
        # (measured with a 2.4 s json.dumps): the gap snapshot is the evidence.
        wd = self.make()
        wd.check(100.00)
        self.state["lag_expected"] = 100.5
        self.assertIsNone(wd.check(100.45))
        self.assertEqual(len(wd.gaps), 1)
        self.assertIn("threads", wd.gaps[0])

    def test_no_deadline_no_capture(self):
        wd = self.make()
        self.state["lag_expected"] = None
        self.assertIsNone(wd.check(105.0))

    def test_stack_failure_is_recorded_not_raised(self):
        wd = SC.StallWatchdog({"lag_t0": 0.0, "lag_expected": 1.0},
                              clock=Clock(), stacks=lambda: 1 / 0)
        capture = wd.check(2.0)
        self.assertIn("ZeroDivisionError", capture["threads"]["error"][0])

    def test_thread_stops_when_the_probe_is_switched_off(self):
        state = {"lag_t0": time.perf_counter(), "lag_probe_off": True}
        wd = SC.StallWatchdog(state, poll_s=0.005).start()
        wd._thread.join(1.0)
        self.assertFalse(wd._thread.is_alive())


class RealThreadTests(unittest.TestCase):
    def test_format_stacks_names_threads_and_frames(self):
        stacks = SC.format_stacks()
        self.assertIn("MainThread", stacks)
        self.assertTrue(any("test_format_stacks_names_threads_and_frames" in line
                            for line in stacks["MainThread"]))

    def test_a_blocked_main_thread_is_captured_with_its_stack(self):
        state = {"lag_t0": time.perf_counter()}
        state["lag_expected"] = time.perf_counter()
        wd = SC.StallWatchdog(state, threshold_s=0.05, poll_s=0.005).start()
        try:
            time.sleep(0.3)                  # the "stall"
        finally:
            wd.stop()
        self.assertGreaterEqual(len(wd.captures), 1)
        main = wd.captures[0]["threads"]["MainThread"]
        self.assertTrue(any("test_a_blocked_main_thread_is_captured" in line
                            for line in main), main)
        self.assertNotIn("ankiscape-stall-watchdog", wd.captures[0]["threads"])


class TimingHooksTests(unittest.TestCase):
    def test_gc_runs_are_timed_and_fast_young_runs_only_counted(self):
        clock = Clock(10.0)
        hooks = SC.TimingHooks({"lag_t0": 0.0}, clock=clock, min_gc_ms=5.0)
        for gen, dur in ((0, 0.001), (2, 0.38), (1, 0.006)):
            hooks.gc_callback("start", {"generation": gen})
            clock.now += dur
            hooks.gc_callback("stop", {"generation": gen, "collected": 7})
        report = hooks.report()
        self.assertEqual([(e["generation"], e["ms"]) for e in report["gc_events"]],
                         [(2, 380.0), (1, 6.0)])
        self.assertEqual(report["gc_events"][0]["at"], 10.001)
        self.assertEqual(report["gc_counts"]["0"]["runs"], 1)
        self.assertEqual(report["gc_counts"]["2"]["max_ms"], 380.0)

    def test_real_gc_collect_is_recorded(self):
        hooks = SC.TimingHooks({"lag_t0": time.perf_counter()})
        hooks.install_gc()
        try:
            gc.collect()
        finally:
            hooks.uninstall_gc()
        self.assertNotIn(hooks.gc_callback, gc.callbacks)
        self.assertTrue(any(e["generation"] == 2
                            for e in hooks.report()["gc_events"]))

    def test_wrapped_method_is_timed_and_restored(self):
        class Journal:
            def save_checkpoint(self, uuid, revision, checkpoint):
                clock.now += 0.25
                return revision

        clock = Clock(5.0)
        original = Journal.save_checkpoint
        hooks = SC.TimingHooks({"lag_t0": 0.0}, clock=clock)
        self.assertTrue(hooks.wrap(Journal, "save_checkpoint", "checkpoint_save"))
        self.assertFalse(hooks.wrap(Journal, "save_checkpoint", "checkpoint_save"))
        self.assertEqual(Journal().save_checkpoint("g", 3, {}), 3)
        calls = hooks.report()["calls"]["checkpoint_save"]
        self.assertEqual(calls[0]["at"], 5.0)
        self.assertEqual(calls[0]["ms"], 250.0)
        self.assertEqual(calls[0]["thread"], threading.current_thread().name)
        hooks.restore()
        self.assertIs(Journal.save_checkpoint, original)

    def test_wrapped_call_that_raises_is_still_timed(self):
        class Worker:
            def _build(self):
                raise RuntimeError("boom")

        hooks = SC.TimingHooks({"lag_t0": 0.0}, clock=Clock())
        hooks.wrap(Worker, "_build", "projection_build")
        with self.assertRaises(RuntimeError):
            Worker()._build()
        self.assertEqual(len(hooks.report()["calls"]["projection_build"]), 1)
        hooks.restore()

    def test_missing_attribute_is_not_wrapped(self):
        hooks = SC.TimingHooks({}, clock=Clock())
        self.assertFalse(hooks.wrap(types.SimpleNamespace(), "nope", "x"))


class DriverWiringTests(unittest.TestCase):
    """_stall_capture_start/_stop in the real driver, in control mode (no
    ankiscape package): the watchdog and gc hooks still run."""

    def setUp(self):
        qt = types.ModuleType("aqt.qt")
        qt.QTimer = types.SimpleNamespace(singleShot=lambda ms, fn: None)
        aqt = types.ModuleType("aqt")
        aqt.qt = qt
        patcher = mock.patch.dict(sys.modules, {"aqt": aqt, "aqt.qt": qt})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.drv = _load("e2e_driver_pkg_under_test",
                         os.path.join(DRIVER_DIR, "__init__.py"), DRIVER_DIR)
        self.addCleanup(sys.modules.pop, "e2e_driver_pkg_under_test", None)

    def test_start_then_stop_reports_and_removes_hooks(self):
        state = {"lag_t0": time.perf_counter(), "stage": "answers",
                 "answers_done": 3}
        state["lag_expected"] = time.perf_counter()
        callbacks_before = len(gc.callbacks)
        self.drv._stall_capture_start(state)
        self.assertNotIn("stall_capture_error", state)
        self.assertEqual(len(gc.callbacks), callbacks_before + 1)
        time.sleep(0.3)
        gc.collect()
        state["lag_probe_off"] = True
        self.drv._stall_capture_stop(state)
        self.assertEqual(len(gc.callbacks), callbacks_before)
        report = state["stall_capture"]
        self.assertGreaterEqual(len(report["captures"]), 1)
        self.assertEqual(report["captures"][0]["phase"], "answers:3")
        self.assertTrue(any(e["generation"] == 2 for e in report["gc_events"]))
        self.assertNotIn("error", report)


class StallNoteTests(unittest.TestCase):
    @staticmethod
    def run_with(lag_ms, lag_at, capture, repetition=0):
        return {"repetition": repetition, "install_addon": True,
                "raw": {"run_mode": "addon", "lag_ms": lag_ms, "lag_at": lag_at,
                        "lag_phase": ["answers:%d" % i for i in range(len(lag_ms))],
                        "stall_capture": capture}}

    def test_stall_note_names_main_frame_and_overlaps(self):
        capture = {
            "captures": [{"at": 29.7, "late_ms": 200.0, "watchdog_gap_ms": 20.0,
                          "threads": {"MainThread": ["a.py:1 f", "b.py:2 tick"]}}],
            "watchdog_gaps": [],
            "gc_events": [{"at": 29.5, "ms": 360.0, "generation": 2,
                           "thread": "projection"}],
            "calls": {"checkpoint_save": [{"at": 12.0, "ms": 40.0,
                                           "thread": "projection"}]}}
        notes = NP._stall_notes([self.run_with([2.0, 385.8], [29.39, 29.876],
                                               capture)])
        self.assertEqual(len(notes), 1)
        self.assertIn("rep0-addon 386 ms ending @29.88s (answers:1)", notes[0])
        self.assertIn("main: b.py:2 tick", notes[0])
        self.assertIn("gc gen2 360 ms @29.50s [projection]", notes[0])
        self.assertNotIn("checkpoint_save", notes[0])

    def test_starved_watchdog_reads_as_gil_held(self):
        capture = {"captures": [], "gc_events": [],
                   "watchdog_gaps": [{"at": 29.9, "gap_ms": 390.0, "threads": {
                       "MainThread": ["x.py:1 tick"],
                       "projection": ["journal.py:564 save_checkpoint",
                                      "encoder.py:257 iterencode"],
                       "Dummy-3": ["threading.py:306 wait"]}}],
                   "calls": {"checkpoint_save": [{"at": 29.5, "ms": 380.0,
                                                  "thread": "projection"}]}}
        notes = NP._stall_notes([self.run_with([385.8], [29.876], capture)])
        self.assertIn("watchdog starved 390 ms (GIL held or process paused)", notes[0])
        self.assertIn("just after: projection: encoder.py:257 iterencode",
                      notes[0])
        self.assertNotIn("Dummy-3", notes[0])
        self.assertIn("checkpoint_save 380 ms @29.50s [projection]", notes[0])

    def test_runs_without_capture_still_note_the_stall(self):
        notes = NP._stall_notes([self.run_with([10.0, 402.4], [1.0, 40.178], None)])
        self.assertEqual(len(notes), 1)
        self.assertIn("no capture", notes[0])
        self.assertIn("overlaps: none recorded", notes[0])

    def test_small_samples_make_no_notes_and_many_are_capped(self):
        self.assertEqual(NP._stall_notes([self.run_with([149.0], [1.0], {})]), [])
        many = NP._stall_notes([self.run_with(
            [200.0 + i for i in range(20)], [float(i) for i in range(20)], {})])
        self.assertEqual(len(many), NP.STALL_NOTES_PER_RUN + 1)
        self.assertIn("8 smaller stalls", many[0])
        self.assertTrue(any("219 ms" in n for n in many))


if __name__ == "__main__":
    unittest.main()
