# dev/rss.py - CURRENT resident-set size, one implementation, dev only.
"""Process RSS in MiB, or None when the platform cannot measure it.

This exists because the same measurement was written three times and drifted.
Two of the three sinks read `resource.getrusage().ru_maxrss`, which is a
HIGH-WATER MARK: it never decreases, so its "slope" is monotonic by
construction and any transient peak anywhere in a run is locked into the
evaluated window forever. Under that metric a perfectly healthy app reports an
ever-rising trend -- which is what produced the false leak verdict the endurance
gate was corrected for. `dev/e2e/driver_addon/__init__.py` was fixed to sample
current resident size; `dev/endurance.py` and `dev/perf_runtime.py` were not,
and both still held the defect.

The implementation below is that fixed one, moved here verbatim so there is a
single copy. Callers must treat None as "not measured" and never as zero: a
missing measurement reported as 0.0 reads like a flat, healthy slope, which is
the failure mode `no_answers_measured` and `no_fixed_phase` exist to prevent.

    win32   GetProcessMemoryInfo -> WorkingSetSize
    darwin  task_info(TASK_BASIC_INFO) -> resident_size
    other   /proc/self/statm

Loaded by absolute path from sibling scripts (`dev/endurance.py`,
`dev/perf_runtime.py`), matching how those tools already load
`dev/endurance_metrics.py`; `dev/` is not a package.
"""
from __future__ import annotations

import os
import sys


def current_rss_mib():
    """Process RSS in MiB, or None when the platform cannot measure it."""
    try:
        if sys.platform == "win32":
            import ctypes
            import ctypes.wintypes as _wt

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", _wt.DWORD),
                    ("PageFaultCount", _wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _PMC()
            counters.cb = ctypes.sizeof(counters)
            process = ctypes.windll.kernel32.GetCurrentProcess()
            ok = 0
            for lib, fn in (("psapi", "GetProcessMemoryInfo"),
                            ("kernel32", "K32GetProcessMemoryInfo")):
                try:
                    func = getattr(getattr(ctypes.windll, lib), fn)
                    func.argtypes = [ctypes.c_void_p,
                                     ctypes.POINTER(_PMC), _wt.DWORD]
                    func.restype = _wt.BOOL
                    ok = func(process, ctypes.byref(counters), counters.cb)
                    if ok:
                        break
                except Exception:
                    continue
            if not ok:
                return None
            return round(counters.WorkingSetSize / (1024 * 1024), 1)
        # darwin/linux: CURRENT resident size, never getrusage().ru_maxrss.
        if sys.platform == "darwin":
            import ctypes
            import ctypes.util
            libc = ctypes.CDLL(ctypes.util.find_library("System")
                               or "/usr/lib/libSystem.B.dylib")

            class _TimeValue(ctypes.Structure):
                _fields_ = [("seconds", ctypes.c_int),
                            ("microseconds", ctypes.c_int)]

            class _MachTaskBasicInfo(ctypes.Structure):
                _fields_ = [("virtual_size", ctypes.c_ulonglong),
                            ("resident_size", ctypes.c_ulonglong),
                            ("resident_size_max", ctypes.c_ulonglong),
                            ("user_time", _TimeValue),
                            ("system_time", _TimeValue),
                            ("policy", ctypes.c_int),
                            ("suspend_count", ctypes.c_int)]

            libc.task_info.argtypes = [ctypes.c_uint, ctypes.c_int,
                                       ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_uint)]
            libc.task_info.restype = ctypes.c_int
            task = ctypes.c_uint.in_dll(libc, "mach_task_self_").value
            info = _MachTaskBasicInfo()
            count = ctypes.c_uint(
                ctypes.sizeof(info) // ctypes.sizeof(ctypes.c_int))
            if libc.task_info(task, 20, ctypes.byref(info),
                              ctypes.byref(count)) != 0:
                return None
            return round(info.resident_size / (1024 * 1024), 1)
        with open("/proc/self/statm", encoding="ascii") as fh:
            pages = int(fh.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024), 1)
    except Exception:
        return None
