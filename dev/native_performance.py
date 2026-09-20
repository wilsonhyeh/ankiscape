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
import re
import shutil
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


def _stuck_shot_order(name: str) -> int:
    match = re.search(r"stuck-(\d+)", name)
    return int(match.group(1)) if match else 10 ** 9


def _archive_run_evidence(base: str, dest_dir: str) -> list:
    """Copy the driver's own diagnostics for one run.

    Watchdog/subprocess timeouts must keep the last heartbeat, the watchdog
    trace, the fatal detail (full traceback), the assertion record, the Anki
    stderr tail and the final stuck screenshot so a later session can name
    the stalled phase. Runs on every outcome, pass or fail."""
    os.makedirs(dest_dir, exist_ok=True)
    copied = []
    singles = [
        ("e2e-heartbeat.json", "heartbeat.json"),
        ("e2e-trace.jsonl", "trace.jsonl"),
        ("e2e-faulthandler.log", "faulthandler.log"),
        ("e2e-fatal.txt", "fatal.txt"),
        ("e2e-assertions.json", "assertions.json"),
        ("e2e-perf-native.json", "perf-native.json"),
        ("relaunch.json", "relaunch.json"),
    ]
    for src_name, dest_name in singles:
        src = os.path.join(base, src_name)
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(dest_dir, dest_name))
            copied.append(dest_name)
    log = os.path.join(base, "anki-stdout.log")
    if os.path.isfile(log):
        with open(log, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 262144))
            data = fh.read()
        with open(os.path.join(dest_dir, "anki-stdout-tail.log"), "wb") as fh:
            fh.write(data)
        copied.append("anki-stdout-tail.log")
    try:
        shots = sorted((n for n in os.listdir(base)
                        if n.startswith("e2e-") and n.endswith(".png")
                        and ("stuck-" in n or "fatal" in n)),
                       key=_stuck_shot_order)
    except OSError:
        shots = []
    for name in shots[-3:]:
        dest_name = name[len("e2e-"):]
        try:
            shutil.copyfile(os.path.join(base, name),
                            os.path.join(dest_dir, dest_name))
            copied.append(dest_name)
        except OSError:
            continue
    return copied


def _run_journey(journey: str, *, install_addon: bool, config: dict,
                 anki: str, qt: str, anki_bin: str,
                 evidence_dir: str = "") -> dict:
    dev = _dev()
    config = dict(config)
    config.setdefault("max_ticks", _max_ticks(config))
    phase_timeout = max(900, config["max_ticks"] // 10 + 300)
    rc = dev._e2e_suite(anki, str(qt), journey, anki_bin, "",
                        install_addon=install_addon, extra_config=config,
                        phase_timeout_s=phase_timeout)
    base = os.path.join(dev.DEV_DIR, "e2e", journey)
    # Metrics truth: an absent, unreadable or empty payload used to collapse
    # into `raw = {}`, which then surfaced as a generic `native_performance =>
    # None` with no way to tell a crashed journey from a real measurement. The
    # state is carried out of here so the failure can name its own cause.
    metrics_path = os.path.join(base, "e2e-perf-native.json")
    raw = {}
    metrics_state = "ok"
    try:
        with open(metrics_path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raw, metrics_state = {}, "missing"
    except (OSError, ValueError) as exc:
        raw, metrics_state = {}, f"unreadable_{exc.__class__.__name__.lower()}"
    if metrics_state == "ok" and not isinstance(raw, dict):
        raw, metrics_state = {}, "not_an_object"
    if metrics_state == "ok" and not raw:
        metrics_state = "empty"
    runtime = {}
    try:
        with open(os.path.join(base, "e2e-assertions.json"),
                  encoding="utf-8") as fh:
            runtime = (json.load(fh) or {}).get("runtime") or {}
    except (OSError, ValueError):
        pass
    evidence = []
    if evidence_dir:
        try:
            evidence = _archive_run_evidence(base, evidence_dir)
        except OSError:
            evidence = []
    return {"journey": journey, "install_addon": install_addon,
            "rc": int(rc), "raw": raw, "runtime": runtime,
            "metrics_state": metrics_state, "metrics_path": metrics_path,
            "evidence": evidence}


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


def _collect_counter(runs, field, mode=None):
    """Sum a driver counter across runs; None when no run reports it.

    A payload from before the counter existed (old shape) makes
    reconciliation not applicable, never a failure."""
    total = 0
    seen = False
    for run in runs:
        if mode and run["raw"].get("run_mode") != mode:
            continue
        if run["rc"] != 0:
            continue
        seen = True
        value = run["raw"].get(field)
        if value is None:
            return None
        total += int(value)
    return total if seen else None


def _run_paired(profile: str, cfg: dict, anki: str, qt: str,
                anki_bin: str, evidence_root: str = "") -> list:
    runs = []
    control_cfg = dict(cfg)
    control_cfg.update({"answers": cfg["answers"], "bulk_ops": 0,
                        "rebuilds_at": [], "mode": "perf"})
    addon_cfg = dict(cfg)
    addon_cfg["mode"] = "perf"
    for repetition in range(int(cfg.get("repetitions", 1))):
        control = _run_journey("native-performance-control",
                               install_addon=False, config=control_cfg,
                               anki=anki, qt=qt, anki_bin=anki_bin,
                               evidence_dir=(os.path.join(
                                   evidence_root,
                                   f"rep{repetition}-control")
                                   if evidence_root else ""))
        addon = _run_journey("native-performance", install_addon=True,
                             config=addon_cfg, anki=anki, qt=qt,
                             anki_bin=anki_bin,
                             evidence_dir=(os.path.join(
                                 evidence_root,
                                 f"rep{repetition}-addon")
                                 if evidence_root else ""))
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
    if endurance_report is not None:
        # Endurance is its own experiment: it has no paired control and no
        # timing budgets, and must not be failed for their absence. The
        # separate native-performance record still enforces every paired
        # responsiveness budget.
        metrics = {
            "endurance_memory": {
                "slope_mib_per_min": endurance_report.get("slope_mib_per_min"),
                "settled_increase_mib": endurance_report.get("settled_increase_mib"),
                "duration_min": endurance_report.get("duration_min"),
                "baseline_rss_mib": endurance_report.get("baseline_rss_mib"),
                "answers": endurance_report.get("answers"),
                "samples": endurance_report.get("sample_count"),
                "window_samples": endurance_report.get("window_samples"),
                "sample_cadence_s": endurance_report.get("sample_cadence_s"),
                "evaluated_phase": endurance_report.get("evaluated_phase"),
                "fixed_span_min": endurance_report.get("fixed_span_min"),
                "trend_warmup_dropped":
                    endurance_report.get("trend_warmup_dropped"),
                "growing_samples": endurance_report.get("growing_samples", 0),
                "growing_rss_change_mib":
                    endurance_report.get("growing_rss_change_mib"),
                "eligible_for_release":
                    endurance_report.get("eligible_for_release"),
                "pass": endurance_report.get("pass"),
            },
        }
        failures.extend(f"endurance:{f}" for f in
                        endurance_report.get("failures") or [])
        if endurance_report.get("pass") is not True:
            failures.append("endurance:not_pass")
        return metrics, failures
    addon_runs = [r for r in runs if r["install_addon"] and r["rc"] == 0]
    control_runs = [r for r in runs if not r["install_addon"] and r["rc"] == 0]
    if not control_runs:
        failures.append("missing_control_runs")

    metrics = {}
    accepts = _collect_field(addon_runs, "accept_ms", mode="addon")
    lag = _collect_perf(addon_runs, "addon")
    control_lag = _collect_perf(control_runs, "control")
    rewards = _collect_field(addon_runs, "reward_ms", mode="addon")
    rebuild_lag = _collect_field(addon_runs, "rebuild_lag_ms", mode="addon")
    rebuild_rewards = _collect_field(addon_runs, "rebuild_reward_ms",
                                     mode="addon")
    shells = _collect_field(addon_runs, "shell_ms", mode="addon")
    warm_shells = shells[1:] if len(shells) > 1 else shells
    rebuilds = _collect_field(addon_runs, "rebuilds_ms", mode="addon")
    colds = [r["raw"].get("cold_ms") for r in addon_runs
             if r["raw"].get("cold_ms") is not None]

    metrics["accepted_answer_completion"] = _summary(accepts)
    metrics["event_loop_lag"] = _summary(lag)
    metrics["event_loop_lag"]["control_p95_ms"] = \
        rel.nearest_rank_percentile(control_lag, 95) if control_lag else None
    metrics["event_loop_lag"]["rebuild_window"] = _summary(rebuild_lag)
    metrics["warm_shell_open"] = _summary(warm_shells)
    metrics["cold_shell_appearance"] = {"ms": min(colds) if colds else None,
                                        "samples": len(colds)}
    metrics["reward_completion"] = _summary(rewards)
    metrics["reward_completion"]["rebuild_window"] = _summary(rebuild_rewards)
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
    required = {"event_loop_lag", "warm_shell_open", "cold_shell_appearance",
                "idle_cpu", "reward_completion", "late_retraction_rebuild"}
    for failure in rel.evaluate_budgets(
            metrics, budgets, enforce_samples=profile == "release",
            required=required):
        if ":target_missed:" in failure:
            continue
        failures.append(failure)

    rebuild_count = int(metrics["late_retraction_rebuild"].get("samples") or 0)
    if rebuild_count > 0 and not rebuild_lag:
        failures.append("rebuild_window_lag:not_measured")
    if rebuild_count == 0 and rebuild_lag:
        failures.append("rebuild_window_lag:unreconciled")
    if rebuild_count == 0 and rebuild_rewards:
        failures.append("rebuild_reward_samples:unreconciled")
    probes = _collect_counter(addon_runs, "lag_probes", mode="addon")
    if probes is not None and probes != len(lag) + len(rebuild_lag):
        failures.append("lag_samples:unreconciled")
    published = _collect_counter(addon_runs, "rewards_published", mode="addon")
    if published is not None and published != len(rewards) + len(rebuild_rewards):
        failures.append("reward_samples:unreconciled")

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


def _fresh_bases() -> list:
    """Remove the scratch profile dirs so this invocation starts clean.

    Both this tool's ordinary mode and its `--endurance-minutes` mode run the
    SAME journey name ("native-performance") and therefore share one profile at
    `<DEV_DIR>/e2e/native-performance`. Nothing cleaned it between runs, and
    reliability.py's lane runs scenarios sequentially in one job, so the
    endurance scenario used to inherit the ordinary perf scenario's populated
    collection and its ~77 MB Evolved journal.

    That inheritance is not cosmetic. Measured on the same machine, same shape:

        clean, 30-min run          slope -3.167 MiB/min   settled 17.3 MiB
        clean + CI env, 30-min     slope -0.080           settled 25.2 MiB
        INHERITED state, 10-min    slope +9.377           settled 42.4 MiB

    ...and CI, which always inherited, reported slope +70.314 / settled 670.8
    and failed. The endurance scenario must measure a fresh profile, not
    whatever the previous scenario left resident.
    """
    dev = _dev()
    removed = []
    for journey in ("native-performance", "native-performance-control"):
        base = os.path.join(dev.DEV_DIR, "e2e", journey)
        if os.path.isdir(base):
            shutil.rmtree(base, ignore_errors=True)
            removed.append(base)
    return removed


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
    parser.add_argument("--evidence-dir", default="",
                        help="archive per-run driver diagnostics here "
                             "(heartbeat, watchdog trace, fatal detail, "
                             "stuck screenshots, Anki stderr tail)")
    args = parser.parse_args(argv)
    for _base in _fresh_bases():
        print(f"native_performance: cleared stale base {_base}")

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
                           anki_bin=args.anki_bin,
                           evidence_dir=(os.path.join(
                               args.evidence_dir,
                               f"endurance-{args.endurance_minutes:g}m")
                               if args.evidence_dir else ""))
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
            # The journey died before writing its endurance payload (or
            # wrote the wrong mode): missing endurance evidence is a
            # failure, never an absent check.
            endurance_report = {
                "profile": args.profile,
                "duration_min": args.endurance_minutes,
                "sample_count": 0, "pass": False,
                "failures": ["endurance_raw_missing"],
            }
    else:
        runs = _run_paired(args.profile, cfg, args.anki, args.qt,
                           args.anki_bin, evidence_root=args.evidence_dir)

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
    # A rep with no usable metrics object is a named failure, never a silent
    # None: the reader has to be able to tell an empty payload from a crash.
    for index, run in enumerate(runs, start=1):
        state = run.get("metrics_state")
        if state and state != "ok":
            failures.append(f"native_performance_metrics_{state} "
                            f"rep={index} path={run.get('metrics_path', '')}")

    payload = {
        "schema": "ankiscape-native-performance", "version": 1,
        "profile": args.profile,
        "eligible_for_release": bool(cfg.get("eligible_for_release")),
        "started_at": int(started), "duration_s": round(time.time() - started, 1),
        "endurance_minutes": args.endurance_minutes,
        "observed": observed, "metrics": metrics,
        "runs": [{"journey": r["journey"], "rc": r["rc"],
                  "runtime": r["runtime"],
                  "raw_len": len(json.dumps(r["raw"])),
                  "metrics_state": r.get("metrics_state"),
                  "evidence": len(r.get("evidence") or [])} for r in runs],
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
