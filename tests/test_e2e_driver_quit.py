# tests/test_e2e_driver_quit.py - the e2e driver does not quit Anki during startup.
"""Anki 23.10 on Qt5/Linux segfaults on exit (code -11) when it is quit while it
is still starting up. A journey that finishes on its first tick quit about 1.6 s
after launch and crashed in 31 of 240 launches; held open about 8 s it crashed
in 0 of 90 (CI experiment 2026-10-01). `_quit` therefore waits for
MIN_UPTIME_BEFORE_QUIT_S since the driver loaded. These tests pin the wait, that
it happens once, that the exit code survives it, and that a missing Qt timer
quits at once instead of hanging."""
from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRIVER = os.path.join(ROOT, "dev", "e2e", "driver_addon", "__init__.py")


def _load_driver():
    spec = importlib.util.spec_from_file_location("e2e_driver_under_test", DRIVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class QuitTests(unittest.TestCase):
    def setUp(self):
        self.timers = []   # (milliseconds, callback) queued by QTimer.singleShot
        self.exits = []    # exit codes passed to QApplication.exit
        self.closed = []   # mw.close() calls
        self.now = [1000.0]

        timers, exits, closed = self.timers, self.exits, self.closed

        class FakeTimer:
            @staticmethod
            def singleShot(ms, fn):
                timers.append((ms, fn))

        class FakeApp:
            def exit(self, code):
                exits.append(code)

        class FakeQApplication:
            @staticmethod
            def instance():
                return FakeApp()

        self.qt = types.ModuleType("aqt.qt")
        self.qt.QTimer = FakeTimer
        self.qt.QApplication = FakeQApplication
        aqt = types.ModuleType("aqt")
        aqt.mw = types.SimpleNamespace(close=lambda: closed.append(True))
        aqt.qt = self.qt
        patcher = mock.patch.dict(sys.modules, {"aqt": aqt, "aqt.qt": self.qt})
        patcher.start()
        self.addCleanup(patcher.stop)

        self.drv = _load_driver()
        self.drv.time = types.SimpleNamespace(time=lambda: self.now[0])
        self.drv._IMPORTED_AT = 1000.0

    def test_floor_is_the_validated_hold(self):
        # The experiment held the journey open ~8 s after its first tick.
        self.assertGreaterEqual(self.drv.MIN_UPTIME_BEFORE_QUIT_S, 8.0)

    def test_an_early_quit_waits_out_the_floor(self):
        self.now[0] = 1001.6                      # 1.6 s after launch
        self.drv._quit(0)
        self.assertEqual(len(self.timers), 1)
        ms, callback = self.timers[0]
        self.assertAlmostEqual(
            ms, (self.drv.MIN_UPTIME_BEFORE_QUIT_S - 1.6) * 1000 + 50, delta=1)
        self.assertEqual(self.exits, [])          # still up
        self.assertEqual(self.closed, [])
        self.now[0] = 1000.0 + self.drv.MIN_UPTIME_BEFORE_QUIT_S + 0.1
        callback()
        self.assertEqual(self.exits, [0])
        self.assertEqual(self.closed, [True])
        self.assertEqual(len(self.timers), 1)     # the wait is not repeated

    def test_the_deferred_quit_always_completes_even_if_it_fires_early(self):
        # A timer that fires a little early (or a clock that moved) must still
        # quit: re-deferring here would leave Anki up forever.
        self.now[0] = 1001.0
        self.drv._quit(0)
        self.timers[0][1]()                        # time has not advanced
        self.assertEqual(self.exits, [0])
        self.assertEqual(self.closed, [True])
        self.assertEqual(len(self.timers), 1)

    def test_a_late_quit_is_immediate(self):
        self.now[0] = 1000.0 + self.drv.MIN_UPTIME_BEFORE_QUIT_S + 20
        self.drv._quit(3)
        self.assertEqual(self.timers, [])
        self.assertEqual(self.exits, [3])
        self.assertEqual(self.closed, [True])

    def test_the_exit_code_survives_the_wait(self):
        self.now[0] = 1001.0
        self.drv._quit(7)
        self.now[0] = 1020.0
        self.timers[0][1]()
        self.assertEqual(self.exits, [7])

    def test_a_repeat_call_cannot_skip_the_floor_and_the_last_code_wins(self):
        self.now[0] = 1001.0
        self.drv._quit(0)
        self.now[0] = 1002.0
        self.drv._quit(1)                          # e.g. a late failure path
        self.assertEqual(len(self.timers), 1)      # one wait, not two
        self.assertEqual(self.exits, [])           # the floor was not skipped
        self.now[0] = 1020.0
        self.timers[0][1]()
        self.assertEqual(self.exits, [1])

    def test_without_a_qt_timer_it_quits_at_once_instead_of_hanging(self):
        del self.qt.QTimer                         # `from aqt.qt import QTimer` fails
        self.now[0] = 1001.0
        self.drv._quit(0)
        self.assertEqual(self.exits, [0])
        self.assertEqual(self.closed, [True])

    def test_the_wait_still_marks_the_phase_as_finished(self):
        # Our own timers and the abort writer key off these during the wait.
        self.now[0] = 1001.0
        self.drv._quit(0)
        self.assertTrue(self.drv._QUITTING)
        self.assertTrue(self.drv._COMPLETED)


if __name__ == "__main__":
    unittest.main()
