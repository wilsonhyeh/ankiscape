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
# Native runs carry two phases: a paced growing history and a fixed-history
# lifecycle. Leak verdicts only apply to the fixed phase; a growing-history
# slope is expected growth, never a leak. A trend check needs at least this
# much fixed-history time and discards this much as cache warm-up before
# measuring (release uses its full 30-minute warm-up instead).
TREND_MIN_FIXED_MIN = 10.0
TREND_WARMUP_MIN = 5.0
# A flat fixed-phase trend is only evidence if the UI churn it measures actually
# ran. The reviewer side already fails an unanswered run (`no_answers_measured`)
# after a deck ran dry and produced "a flat, entirely plausible-looking trend --
# and a pass" while measuring an idle application; the lifecycle stage had no
# equivalent. Without a floor, any change that stopped opening the shell (or
# held it open indefinitely) would read as a leak fix. A real fixed phase is
# ~15 minutes, so even a 8-second cycle yields >100; this only catches collapse.
MIN_LIFECYCLE_CYCLES = 100


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
    # A trend run must have actually reviewed something. The driver samples
    # memory every 30 s whether or not the answer loop is running, so a run
    # whose deck emptied produced a flat, entirely plausible-looking trend --
    # and a pass -- while measuring an idle application. Never report that as
    # evidence: an unanswered run fails loudly instead of going green.
    answers = 0
    for s in samples:
        if isinstance(s, dict) and _finite(s.get("answers")):
            answers = max(answers, int(float(s["answers"])))
    if trend and answers <= 0:
        failures.append("no_answers_measured")
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
            "baseline_rss_mib": None, "answers": answers,
            "eligible_for_release": False,
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
            "baseline_rss_mib": None, "answers": answers,
            "eligible_for_release": False,
            "pass": False, "failures": failures,
        }
    valid.sort(key=lambda s: float(s["at_s"]))
    # Native runs sample two phases. Evaluate the fixed-history lifecycle;
    # the paced growing history is informational (its slope is expected).
    fixed = [s for s in valid if isinstance(s, dict)
             and str(s.get("phase", "")) == "fixed"]
    growing = [s for s in valid if isinstance(s, dict)
               and str(s.get("phase", "")) == "growing"]
    # The churn guard, the leak evaluation and the warm-up trim all live inside
    # `if len(fixed) >= 2` below, so a trend run that measured phases and
    # produced no fixed one -- the shell was never opened -- skipped every one
    # of them and reported `evaluated_phase: "all"` with pass=true. That is the
    # same false green `no_answers_measured` and `no_lifecycle_cycles` exist to
    # kill, one level up: "we did not measure the lifecycle" is not "the
    # lifecycle is clean". A phase-less series is a different case and is still
    # evaluated whole (`series = valid`); this keys on a run that HAS phase
    # information and no fixed-phase sample to judge.
    phase_aware = fixed or growing
    if trend and phase_aware and not fixed:
        failures.append(f"no_fixed_phase:{len(growing)}growing")
    series = fixed if len(fixed) >= 2 else valid
    fixed_span_min = None
    trend_warmup_dropped = 0
    fixed_cycles = 0
    forced_cycles = 0
    if len(fixed) >= 2:
        fixed_span_min = (float(fixed[-1]["at_s"])
                          - float(fixed[0]["at_s"])) / 60.0
        if trend and not release and fixed_span_min < TREND_MIN_FIXED_MIN:
            failures.append(
                f"trend_fixed_span:{round(fixed_span_min, 1)}"
                f"<{TREND_MIN_FIXED_MIN}")
        if trend and not release:
            # Discard the cache warm-up ramp; the nightly verdict is about
            # the settled trend, not the first minutes of a fresh profile.
            warm_cut = float(fixed[0]["at_s"]) + TREND_WARMUP_MIN * 60.0
            trimmed = [s for s in series
                       if float(s["at_s"]) >= warm_cut]
            if len(trimmed) >= 2:
                trend_warmup_dropped = len(series) - len(trimmed)
                series = trimmed
        if trend:
            # Fail a flat trend the churn never actually produced. Samples
            # predating the `cycles` field read as 0, which fails loudly rather
            # than passing a run whose churn is unmeasured.
            fixed_cycles = max(
                (int(s.get("cycles", 0) or 0) for s in fixed), default=0)
            forced_cycles = max(
                (int(s.get("forced", 0) or 0) for s in fixed), default=0)
            if fixed_cycles < MIN_LIFECYCLE_CYCLES:
                failures.append(f"no_lifecycle_cycles:{fixed_cycles}")
    if trend:
        duration_s = max(float(series[-1]["at_s"]), duration_min * 60.0)
        window_start_s = max(0.0, duration_s - window_min * 60.0)
    else:
        cut = max(1, int(len(series) * 0.3))
        window_start_s = float(series[min(cut, len(series) - 1)]["at_s"])
    window = [s for s in series if float(s["at_s"]) >= window_start_s]
    before = [s for s in series if float(s["at_s"]) < window_start_s]
    baseline = float(before[-1]["rss_mib"]) if before \
        else float(window[0]["rss_mib"])
    if len(window) < 2:
        failures.append(f"insufficient_window_samples:{len(window)}")
        return {
            "profile": profile, "duration_min": duration_min,
            "sample_count": len(valid), "window_min": window_min,
            "slope_mib_per_min": None, "settled_increase_mib": None,
            "baseline_rss_mib": baseline, "answers": answers,
            "eligible_for_release": False,
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

    eligible = (release and duration_min >= warmup_min + window_min
                and (fixed_span_min is None
                     or fixed_span_min >= warmup_min + window_min))
    growing_change = None
    if len(growing) >= 2:
        growing_change = round(
            float(growing[-1]["rss_mib"]) - float(growing[0]["rss_mib"]), 1)
    return {
        "profile": profile, "duration_min": duration_min,
        "sample_count": len(valid), "window_min": window_min,
        "window_samples": len(window),
        "evaluated_phase": "fixed" if len(fixed) >= 2 else "all",
        "fixed_samples": len(fixed), "fixed_span_min":
            round(fixed_span_min, 1) if fixed_span_min is not None else None,
        "trend_warmup_dropped": trend_warmup_dropped,
        "fixed_cycles": fixed_cycles,
        "fixed_cycles_min": MIN_LIFECYCLE_CYCLES,
        "forced_cycles": forced_cycles,
        "growing_samples": len(growing),
        "growing_rss_change_mib": growing_change,
        "slope_mib_per_min": round(slope, 3),
        "settled_increase_mib": round(settled, 1),
        "baseline_rss_mib": round(baseline, 1),
        "sample_cadence_s": round(cadence, 1),
        "answers": answers,
        "eligible_for_release": eligible,
        "pass": not failures, "failures": failures,
    }
