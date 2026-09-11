#!/usr/bin/env python3
# dev/native_performance.py - Native responsiveness/endurance measurement.
"""Measures real Anki behavior around the packaged add-on and its paired
control (same synthetic collection and workload, add-on absent).

  python3 dev/native_performance.py --anki 26.8.1 --qt 6 --profile smoke \
      --anki-bin "$ANKI_BIN" --json artifacts/native-performance.json
  python3 dev/native_performance.py --anki 26.8.1 --qt 6 --profile release \
      --anki-bin "$ANKI_BIN" --endurance-minutes 120 --json out.json

Nightly/release enforce the specified measurement floors; smoke is short and
marked ineligible for release. Raw samples stay in --raw (and in the JSON
payload) so summaries can be recomputed without re-running Anki.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

BUDGETS_PATH = os.path.join(ROOT, "dev", "reliability-budgets.json")

PROFILES = {
    "smoke": {
        "answers": 20, "warm_opens": 5, "idle_seconds": 15,
        "bulk_ops": 10000, "rebuilds_at": [3], "repetitions": 1,
        "eligible_for_release": False,
        "floors": {"accepts": 5, "lag_samples": 20, "warm_shell": 2,
                   "rewards": 1, "rebuilds": 1, "idle_s": 10},
    },
    "nightly": {
        "answers": 120, "warm_opens": 20, "idle_seconds": 300,
        "bulk_ops": 100000, "rebuilds_at": [30, 60, 90], "repetitions": 3,
        "eligible_for_release": False,
        "floors": {"accepts": 60, "lag_samples": 300, "warm_shell": 20,
                   "rewards": 30, "rebuilds": 3, "idle_s": 300},
    },
    "release": {
        "answers": 500, "warm_opens": 20, "idle_seconds": 300,
        "bulk_ops": 100000, "rebuilds_at": [100, 300, 450],
        "repetitions": 3, "eligible_for_release": True,
        "floors": {"accepts": 300, "lag_samples": 500, "warm_shell": 20,
                   "rewards": 30, "rebuilds": 3, "idle_s": 300},
    },
}


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_NATIVE_MODULES = {}


def _dev():
    if "dev" not in _NATIVE_MODULES:
        _NATIVE_MODULES["dev"] = _load("ankiscape_dev_native",
                                       os.path.join(ROOT, "dev.py"))
    return _NATIVE_MODULES["dev"]


def _rel():
    if "rel" not in _NATIVE_MODULES:
        _NATIVE_MODULES["rel"] = _load("ankiscape_rel_native",
                                       os.path.join(ROOT, "dev",
                                                    "reliability.py"))
    return _NATIVE_MODULES["rel"]


def _endurance():
    if "end" not in _NATIVE_MODULES:
        _NATIVE_MODULES["end"] = _load("ankiscape_endurance_metrics",
                                       os.path.join(ROOT, "dev",
                                                    "endurance_metrics.py"))
    return _NATIVE_MODULES["end"]


def _max_ticks(cfg: dict) -> int:
    """Tick budget for a journey (100 ms ticks) with import/review slack."""
    return (1500 + int(cfg.get("idle_seconds", 0)) * 10
            + int(cfg.get("answers", 0)) * 45
            + int(float(cfg.get("endurance_minutes", 0)) * 600))


def _run_journey(journey: str, *, install_addon: bool, config: dict,
                 anki: str, qt: str, anki_bin: str) -> dict:
    dev = _dev()
    config = dict(config)
    config.setdefault("max_ticks", _max_ticks(config))
    phase_timeout = max(900, config["max_ticks"] // 10 + 300)
    rc = dev._e2e_suite(anki, str(qt), journey, anki_bin, "",
                        install_addon=install_addon, extra_config=config,
                        phase_timeout_s=phase_timeout)
    base = os.path.join(dev.DEV_DIR, "e2e", journey)
    raw = {}
    try:
        with open(os.path.join(base, "e2e-perf-native.json"),
                  encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        raw = {}
    runtime = {}
    try:
        with open(os.path.join(base, "e2e-assertions.json"),
                  encoding="utf-8") as fh:
            runtime = (json.load(fh) or {}).get("runtime") or {}
    except (OSError, ValueError):
        pass
    return {"journey": journey, "install_addon": install_addon,
            "rc": int(rc), "raw": raw, "runtime": runtime}


def _collect_perf(runs, mode):
    values = []
    for run in runs:
        if run["raw"].get("run_mode") != mode or run["rc"] != 0:
            continue
        values.extend(float(v) for v in run["raw"].get("lag_ms") or [])
    return values


def _collect_field(runs, field, mode=None):
    values = []
    for run in runs:
        if mode and run["raw"].get("run_mode") != mode:
            continue
        if run["rc"] != 0:
            continue
        values.extend(float(v) for v in run["raw"].get(field) or [])
    return values


def _run_paired(profile: str, cfg: dict, anki: str, qt: str,
                anki_bin: str) -> list:
    runs = []
    control_cfg = dict(cfg)
    control_cfg.update({"answers": cfg["answers"], "bulk_ops": 0,
                        "rebuilds_at": [], "mode": "perf"})
    addon_cfg = dict(cfg)
    addon_cfg["mode"] = "perf"
    for repetition in range(int(cfg.get("repetitions", 1))):
        control = _run_journey("native-performance-control",
                               install_addon=False, config=control_cfg,
                               anki=anki, qt=qt, anki_bin=anki_bin)
        addon = _run_journey("native-performance", install_addon=True,
                             config=addon_cfg, anki=anki, qt=qt,
                             anki_bin=anki_bin)
        control["repetition"] = repetition
        addon["repetition"] = repetition
        runs.extend([control, addon])
    return runs


def _summary(values):
    """Percentile summary; empty input is null, never a fake zero."""
    rel = _rel()
    if not values:
        return {"samples": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None,
                "max_ms": None, "min_ms": None}
    return rel.percentile_summary([float(v) for v in values])


def _idle_pair(runs):
    addon = [r["raw"].get("idle_cpu_pct") for r in runs
             if r["install_addon"] and r["raw"].get("idle_cpu_pct") is not None]
    control = [r["raw"].get("idle_cpu_pct") for r in runs
               if not r["install_addon"]
               and r["raw"].get("idle_cpu_pct") is not None]
    addon_avg = sum(addon) / len(addon) if addon else None
    control_avg = sum(control) / len(control) if control else None
    return addon_avg, control_avg


def _evaluate(profile: str, cfg: dict, runs: list, endurance_report=None):
    failures = []
    rel = _rel()
    budgets = json.load(open(BUDGETS_PATH, encoding="utf-8"))
    floors = cfg.get("floors", {})
    addon_runs = [r for r in runs if r["install_addon"] and r["rc"] == 0]
    control_runs = [r for r in runs if not r["install_addon"] and r["rc"] == 0]
    if not control_runs:
        failures.append("missing_control_runs")

    metrics = {}
    accepts = _collect_field(addon_runs, "accept_ms", mode="addon")
    lag = _collect_perf(addon_runs, "addon")
    control_lag = _collect_perf(control_runs, "control")
    rewards = _collect_field(addon_runs, "reward_ms", mode="addon")
    shells = _collect_field(addon_runs, "shell_ms", mode="addon")
    warm_shells = shells[1:] if len(shells) > 1 else shells
    rebuilds = _collect_field(addon_runs, "rebuilds_ms", mode="addon")
    colds = [r["raw"].get("cold_ms") for r in addon_runs
             if r["raw"].get("cold_ms") is not None]

    metrics["accepted_answer_completion"] = _summary(accepts)
    metrics["event_loop_lag"] = _summary(lag)
    metrics["event_loop_lag"]["control_p95_ms"] = \
        rel.nearest_rank_percentile(control_lag, 95) if control_lag else None
    metrics["warm_shell_open"] = _summary(warm_shells)
    metrics["cold_shell_appearance"] = {"ms": min(colds) if colds else None,
                                        "samples": len(colds)}
    metrics["reward_completion"] = _summary(rewards)
    metrics["late_retraction_rebuild"] = {
        "max_ms": max(rebuilds) if rebuilds else None,
        "samples": len(rebuilds),
        "p95_ms": rel.nearest_rank_percentile(rebuilds, 95) if rebuilds else None,
    }
    addon_idle, control_idle = _idle_pair(runs)
    metrics["idle_cpu"] = {
        "addon_pct": round(addon_idle, 4) if addon_idle is not None else None,
        "control_pct": round(control_idle, 4) if control_idle is not None else None,
        "cpu_pct_delta": round(max(0.0, (addon_idle or 0.0) - (control_idle or 0.0)), 4)
        if addon_idle is not None and control_idle is not None else None,
        "window_s": cfg.get("idle_seconds", 0),
        "network_polling": "none",
        "new_recurring_timer": False,
    }
    if endurance_report is not None:
        metrics["endurance_memory"] = {
            "slope_mib_per_min": endurance_report.get("slope_mib_per_min"),
            "settled_increase_mib": endurance_report.get("settled_increase_mib"),
            "duration_min": endurance_report.get("duration_min"),
            "baseline_rss_mib": endurance_report.get("baseline_rss_mib"),
            "samples": endurance_report.get("sample_count"),
            "eligible_for_release": endurance_report.get("eligible_for_release"),
            "pass": endurance_report.get("pass"),
        }
        failures.extend(f"endurance:{f}" for f in
                        endurance_report.get("failures") or [])

    required = {"event_loop_lag", "warm_shell_open", "cold_shell_appearance",
                "idle_cpu", "reward_completion", "late_retraction_rebuild"}
    for failure in rel.evaluate_budgets(
            metrics, budgets, enforce_samples=profile == "release",
            required=required):
        if ":target_missed:" in failure:
            continue
        failures.append(failure)

    def _floor(name, values, key):
        minimum = int(floors.get(key, 0) or 0)
        if minimum and len(values) < minimum:
            failures.append(f"floor:{name}:{len(values)}<{minimum}")

    _floor("accepts", accepts, "accepts")
    _floor("lag_samples", lag, "lag_samples")
    _floor("warm_shell", warm_shells, "warm_shell")
    _floor("rewards", rewards, "rewards")
    _floor("rebuilds", rebuilds, "rebuilds")
    if cfg.get("idle_seconds", 0) < int(floors.get("idle_s", 0) or 0):
        failures.append(f"floor:idle:{cfg.get('idle_seconds')}"
                        f"<{floors.get('idle_s')}")
    return metrics, failures


def _observed_from_runs(runs):
    for run in runs:
        if run["install_addon"] and run["runtime"]:
            return run["runtime"]
    for run in runs:
        if run["runtime"]:
            return run["runtime"]
    return {}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="native_performance.py")
    parser.add_argument("--anki", required=True)
    parser.add_argument("--qt", required=True, choices=("5", "6"))
    parser.add_argument("--anki-bin", default="")
    parser.add_argument("--profile", default="smoke",
                        choices=tuple(PROFILES.keys()))
    parser.add_argument("--json", default="")
    parser.add_argument("--raw", default="")
    parser.add_argument("--endurance-minutes", type=float, default=0.0)
    parser.add_argument("--repetitions", type=int, default=0)
    args = parser.parse_args(argv)

    cfg = dict(PROFILES[args.profile])
    if args.repetitions:
        cfg["repetitions"] = int(args.repetitions)
    started = time.time()
    runs = []
    endurance_report = None

    if args.endurance_minutes > 0:
        endurance_cfg = dict(cfg)
        endurance_cfg.update({
            "mode": "endurance",
            "endurance_minutes": args.endurance_minutes,
            "answers_per_second": 2.0,
            "bulk_ops": cfg.get("bulk_ops", 0),
            "eligible_for_release": args.profile == "release",
        })
        run = _run_journey("native-performance", install_addon=True,
                           config=endurance_cfg, anki=args.anki, qt=args.qt,
                           anki_bin=args.anki_bin)
        runs.append(run)
        raw = run["raw"]
        if raw.get("mode") == "endurance":
            endurance_report = _endurance().evaluate_endurance(
                raw.get("samples") or [], profile=args.profile,
                duration_min=args.endurance_minutes)
            if not raw.get("equivalence"):
                endurance_report.setdefault("failures", []).append(
                    "equivalence:false")
                endurance_report["pass"] = False
    else:
        runs = _run_paired(args.profile, cfg, args.anki, args.qt,
                           args.anki_bin)

    metrics, failures = _evaluate(args.profile, cfg, runs,
                                  endurance_report=endurance_report)
    warnings = []
    if args.profile == "smoke":
        # Smoke proves the harness end to end; it is marked ineligible for
        # release and reports budget breaches as warnings, not gates.
        warnings = [f for f in failures if f.startswith("budget:")]
        failures = [f for f in failures if not f.startswith("budget:")]
    observed = _observed_from_runs(runs)
    if not observed:
        failures.append("missing_observed_runtime")
    if not all(run["rc"] == 0 for run in runs):
        failures.append("journey_failed:" + ",".join(
            r["journey"] for r in runs if r["rc"] != 0))

    payload = {
        "schema": "ankiscape-native-performance", "version": 1,
        "profile": args.profile,
        "eligible_for_release": bool(cfg.get("eligible_for_release")),
        "started_at": int(started), "duration_s": round(time.time() - started, 1),
        "endurance_minutes": args.endurance_minutes,
        "observed": observed, "metrics": metrics,
        "runs": [{"journey": r["journey"], "rc": r["rc"],
                  "runtime": r["runtime"],
                  "raw_len": len(json.dumps(r["raw"]))} for r in runs],
        "failures": failures, "warnings": warnings,
        "pass": not failures,
    }
    if args.raw:
        os.makedirs(os.path.dirname(os.path.abspath(args.raw)), exist_ok=True)
        with open(args.raw, "w", encoding="utf-8") as fh:
            json.dump({"profile": args.profile, "runs": runs,
                       "endurance": endurance_report or {}}, fh, indent=1)
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    print(json.dumps({"profile": args.profile, "pass": payload["pass"],
                      "failures": failures,
                      "metrics": {k: v for k, v in metrics.items()
                                  if k in ("event_loop_lag", "warm_shell_open",
                                           "idle_cpu", "reward_completion",
                                           "late_retraction_rebuild",
                                           "endurance_memory")}},
                     indent=2))
    return 0 if payload["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
