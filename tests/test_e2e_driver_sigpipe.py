# tests/test_e2e_driver_sigpipe.py - the e2e driver records SIGPIPE, never sets it.
"""macOS-26 lanes die with exit -13 (SIGPIPE) and no traceback. CPython
normally ignores SIGPIPE, so the driver records the disposition it finds inside
Anki (evidence only) and must not change it."""
from __future__ import annotations

import importlib.util
import os
import signal
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRIVER = os.path.join(ROOT, "dev", "e2e", "driver_addon", "__init__.py")


def _load_driver():
    spec = importlib.util.spec_from_file_location("e2e_driver_sigpipe", DRIVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(hasattr(signal, "SIGPIPE"), "no SIGPIPE on this platform")
class SigpipeDispositionTests(unittest.TestCase):
    def test_reports_ignore_and_default(self):
        driver = _load_driver()
        with mock.patch.object(signal, "getsignal", return_value=signal.SIG_IGN):
            self.assertEqual(driver._sigpipe_disposition(), "SIG_IGN")
        with mock.patch.object(signal, "getsignal", return_value=signal.SIG_DFL):
            self.assertEqual(driver._sigpipe_disposition(), "SIG_DFL")

    def test_reports_a_python_handler_by_repr(self):
        driver = _load_driver()

        def handler(signum, frame):
            pass

        with mock.patch.object(signal, "getsignal", return_value=handler):
            self.assertEqual(driver._sigpipe_disposition(), repr(handler))

    def test_lookup_failure_is_recorded_not_raised(self):
        driver = _load_driver()
        with mock.patch.object(signal, "getsignal", side_effect=ValueError("x")):
            self.assertTrue(driver._sigpipe_disposition().startswith("error:"))

    def test_recording_does_not_change_the_disposition(self):
        before = signal.getsignal(signal.SIGPIPE)
        driver = _load_driver()
        self.assertEqual(signal.getsignal(signal.SIGPIPE), before)
        self.assertIn(driver.RESULT["sigpipe"], ("SIG_IGN", "SIG_DFL")
                      if before in (signal.SIG_IGN, signal.SIG_DFL)
                      else (repr(before),))


class SigpipeUnavailableTests(unittest.TestCase):
    def test_platform_without_sigpipe_reports_unavailable(self):
        driver = _load_driver()
        real = signal.SIGPIPE if hasattr(signal, "SIGPIPE") else None
        try:
            if real is not None:
                del signal.SIGPIPE
            self.assertEqual(driver._sigpipe_disposition(), "unavailable")
        finally:
            if real is not None:
                signal.SIGPIPE = real


if __name__ == "__main__":
    unittest.main()
