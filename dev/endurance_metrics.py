#!/usr/bin/env python3
# dev/endurance_metrics.py - Pure endurance sample statistics (dev only).
"""Dictionary-shaped endurance samples in, slope/settled verdict out.

The native driver and the pure engine smoke share this helper so an
insufficient run can never be reported as a zero-slope success. Release
measurements only count the final 30-minute window after a fixed 30-minute
warm-up; a nightly 30-minute run is a trend check and is explicitly marked
ineligible for release evidence.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List

SLOPE_LIMIT_MIB_PER_MIN = 1.0
SETTLED_LIMIT_MIB = 50.0
RELEASE_WARMUP_MIN = 30.0
RELEASE_WINDOW_MIN = 30.0
SAMPLE_INTERVAL_S = 30.0


def _finite(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return not (math.isnan(number) or math.isinf(number))


def _sample_errors(samples: List[Any]) -> List[str]:
    errors: List[str] = []
    last_at = None
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            errors.append(f"sample_not_dict:{index}")
            continue
        for field in ("at_s", "rss_mib"):
            if field not in sample:
                errors.append(f"sample_missing:{index}:{field}")
            elif not _finite(sample.get(field)):
                errors.append(f"sample_invalid:{index}:{field}")
        if _finite(sample.get("at_s")):
            at = float(sample["at_s"])
            if at < 0:
                errors.append(f"sample_negative_time:{index}")
            if last_at is not None and at <= last_at:
                errors.append(f"sample_not_monotonic:{index}")
            last_at = at
        if _finite(sample.get("rss_mib")):
            if float(sample["rss_mib"]) < 0:
                errors.append(f"sample_negative_rss:{index}")
    return errors


def evaluate_endurance(samples: List[Any], *, profile: str,
                       duration_min: float,
                       warmup_min: float = RELEASE_WARMUP_MIN,
                       window_min: float = RELEASE_WINDOW_MIN,
                       slope_limit: float = SLOPE_LIMIT_MIB_PER_MIN,
                       settled_limit: float = SETTLED_LIMIT_MIB,
                       sample_interval_s: float = SAMPLE_INTERVAL_S) -> Dict[str, Any]:
    """Evaluate one endurance series.

    Returns a summary dict with `failures`; `pass` is True only when every
    requirement held. `eligible_for_release` is True only for release runs
    that met the warm-up + window duration.
    """
    failures: List[str] = []
    errors = _sample_errors(samples)
    failures.extend(errors)
    duration_min = float(duration_min or 0.0)

    release = profile == "release"
    trend = profile in ("nightly", "release")
    if release and duration_min < warmup_min + window_min:
        failures.append(
            f"release_window_required:{duration_min}<{warmup_min + window_min}")
    elif not release and trend and duration_min < window_min:
        failures.append(f"trend_window_required:{duration_min}<{window_min}")
    if len(samples) < 2:
        failures.append(f"insufficient_samples:{len(samples)}")
        return {
            "profile": profile, "duration_min": duration_min,
            "sample_count": len(samples), "window_min": None,
            "slope_mib_per_min": None, "settled_increase_mib": None,
            "baseline_rss_mib": None, "eligible_for_release": False,
            "pass": False, "failures": failures,
        }

    valid = [s for s in samples
             if isinstance(s, dict) and _finite(s.get("at_s"))
             and _finite(s.get("rss_mib"))]
    if not valid:
        failures.append("no_valid_samples")
        return {
            "profile": profile, "duration_min": duration_min,
            "sample_count": 0, "window_min": None,
            "slope_mib_per_min": None, "settled_increase_mib": None,
            "baseline_rss_mib": None, "eligible_for_release": False,
            "pass": False, "failures": failures,
        }
    valid.sort(key=lambda s: float(s["at_s"]))
    if trend:
        duration_s = max(float(valid[-1]["at_s"]), duration_min * 60.0)
        window_start_s = max(0.0, duration_s - window_min * 60.0)
    else:
        cut = max(1, int(len(valid) * 0.3))
        window_start_s = float(valid[min(cut, len(valid) - 1)]["at_s"])
    window = [s for s in valid if float(s["at_s"]) >= window_start_s]
    before = [s for s in valid if float(s["at_s"]) < window_start_s]
    baseline = float(before[-1]["rss_mib"]) if before \
        else float(window[0]["rss_mib"])
    if len(window) < 2:
        failures.append(f"insufficient_window_samples:{len(window)}")
        return {
            "profile": profile, "duration_min": duration_min,
            "sample_count": len(valid), "window_min": window_min,
            "slope_mib_per_min": None, "settled_increase_mib": None,
            "baseline_rss_mib": baseline, "eligible_for_release": False,
            "pass": False, "failures": failures,
        }

    t0, t1 = float(window[0]["at_s"]), float(window[-1]["at_s"])
    minutes = max(0.001, (t1 - t0) / 60.0)
    slope = (float(window[-1]["rss_mib"]) - float(window[0]["rss_mib"])) / minutes
    settled = max(float(s["rss_mib"]) for s in window) - baseline
    cadence = minutes * 60.0 / max(1, len(window) - 1)
    if trend and cadence > sample_interval_s * 2.5:
        failures.append(f"sample_cadence:{round(cadence, 1)}s")

    if slope > slope_limit:
        failures.append(f"slope:{round(slope, 3)}>{slope_limit}")
    if settled > settled_limit:
        failures.append(f"settled:{round(settled, 1)}>{settled_limit}")

    eligible = release and duration_min >= warmup_min + window_min
    return {
        "profile": profile, "duration_min": duration_min,
        "sample_count": len(valid), "window_min": window_min,
        "window_samples": len(window),
        "slope_mib_per_min": round(slope, 3),
        "settled_increase_mib": round(settled, 1),
        "baseline_rss_mib": round(baseline, 1),
        "sample_cadence_s": round(cadence, 1),
        "eligible_for_release": eligible,
        "pass": not failures, "failures": failures,
    }
