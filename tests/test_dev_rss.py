# tests/test_dev_rss.py - the shared current-RSS measurement.
"""`dev/rss.py` exists because the same measurement was written three times and
two of the copies read `getrusage().ru_maxrss`, a HIGH-WATER MARK whose slope
is monotonic by construction. This pins the two properties that matter: the
value is a real current measurement, and a platform that cannot measure returns
None rather than a zero that reads like a healthy flat slope.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load(name: str, relative: str):
    path = os.path.join(ROOT, relative)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RSS = _load("ankiscape_dev_rss_under_test", "dev/rss.py")


class DevRssTests(unittest.TestCase):
    def test_measures_current_resident_size(self):
        # The three platforms CI runs plus this machine, which is one of them.
        value = RSS.current_rss_mib()
        self.assertIsNotNone(
            value, "no measurement on a supported platform is a defect, "
                   "not an acceptable result")
        self.assertIsInstance(value, float)
        self.assertGreater(value, 0.0)
        # A Python process running the test suite is under a few hundred MiB;
        # this catches a unit error (bytes, KiB, pages) rather than a leak.
        self.assertLess(value, 4096.0, "value looks like the wrong unit")

    def test_falls_back_to_none_rather_than_zero(self):
        # A fake 0.0 for "could not measure" is indistinguishable from a
        # healthy flat slope -- the exact false green the endurance guards
        # exist to prevent. Force the platform seam to fail.
        #
        # Faking sys.platform alone is NOT enough on Linux, which is where the
        # build lane runs: the fallback reads /proc/self/statm and that file
        # really exists there, so an earlier version of this test asserted
        # None and got a real 49.2 MiB measurement back from CI. The seam has
        # to be broken, not merely relabelled.
        import builtins

        real_open = builtins.open

        def _no_statm(file, *args, **kwargs):
            if str(file) == "/proc/self/statm":
                raise OSError("no such file (simulated unmeasurable platform)")
            return real_open(file, *args, **kwargs)

        original_platform = RSS.sys.platform
        try:
            RSS.sys.platform = "sunos5"     # neither win32 nor darwin
            builtins.open = _no_statm
            value = RSS.current_rss_mib()
        finally:
            builtins.open = real_open
            RSS.sys.platform = original_platform
        self.assertIsNone(value)

    def test_dev_tools_do_not_read_the_high_water_mark(self):
        # The regression that matters: no dev tool may SAMPLE ru_maxrss again.
        # Comments and prose are stripped first, because dev/rss.py names
        # getrusage deliberately -- to record why it must not be used. A check
        # on the raw text would fail on that explanation instead of on a call.
        import tokenize

        for relative in ("dev/endurance.py", "dev/perf_runtime.py",
                         "dev/rss.py"):
            path = os.path.join(ROOT, relative)
            with open(path, "rb") as handle:
                tokens = []
                for token in tokenize.tokenize(handle.readline):
                    if token.type in (tokenize.COMMENT, tokenize.STRING):
                        continue
                    tokens.append(token.string)
            code = " ".join(tokens)
            self.assertNotIn(
                "getrusage", code,
                f"{relative} samples getrusage(); ru_maxrss is a high-water "
                f"mark and its slope is monotonic by construction")
            self.assertNotIn("ru_maxrss", code, relative)
