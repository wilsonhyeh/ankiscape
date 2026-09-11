#!/usr/bin/env python3
# dev/perf_runtime.py - Native/pure performance measurements (dev only).
"""Measures the plan's runtime budgets against real engine objects.

Profiles (sample counts per measured history size):
  baseline  - 0/1k/10k x 20, 100k x 3 (capture before optimizations land)
  nightly   - 0/1k/10k x 200, 100k x 10
  release   - every size x 500 (the budget's sample floor)

Measured (pure Python, tmp journal, synthetic data seeded outside timed
regions; every timed accepted answer goes through EvolvedEngine.credit_direct
and its projection read, and the final state is compared with a full reducer
replay):
  - accepted_answer_hook: credit_direct wall time at each history size
  - reward_completion: durable append -> published projection
  - warm_review_scaling: p95 ratio 100k / 1k
  - late_retraction_rebuild: 100k ops + first-award retraction rebuild
  - warm_shell_open / cold_shell_appearance: menu model build times
  - endurance_memory: optional RSS/object slope (--endurance-minutes)

Native-only budgets (event-loop lag, CPU) are measured by the native driver
and merged into the lane record; this tool records them as absent.

Exit status: 0 when measurements completed, 1 when a budget failed under a
gating profile (nightly/release). `--no-gate` records without failing.
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import statistics
import sys
import tempfile
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from evolved.data import load_rules  # noqa: E402
from evolved.engine import EngineConfig, EvolvedEngine  # noqa: E402
from evolved.journal import Journal  # noqa: E402
from evolved.projection_worker import ProjectionWorker  # noqa: E402
from evolved.reducer import replay  # noqa: E402
from evolved.ui.menu_model import (achievement_rows, hiscores_status,  # noqa: E402
                                   skill_rows)

PROFILES = {
    "baseline": {"small": 20, "large": 3},
    "nightly": {"small": 200, "large": 10},
    "release": {"small": 500, "large": 500},
}
HISTORY_SIZES = (0, 1000, 10000, 100000)
BUDGETS = json.load(open(os.path.join(ROOT, "dev", "reliability-budgets.json"),
                         encoding="utf-8"))["budgets"]


def nearest_rank_percentile(values, percentile: float) -> float:
    """Nearest-rank percentile: sorted x[ceil(p/100 * n)], no interpolation."""
    if not values:
        return 0.0
    xs = sorted(float(v) for v in values)
    import math

    rank = max(1, math.ceil((percentile / 100.0) * len(xs)))
    return xs[min(rank, len(xs)) - 1]


def _summary_ms(values):
    return {
        "samples": len(values),
        "p50_ms": round(nearest_rank_percentile(values, 50), 3),
        "p95_ms": round(nearest_rank_percentile(values, 95), 3),
        "p99_ms": round(nearest_rank_percentile(values, 99), 3),
        "max_ms": round(max(values), 3) if values else 0.0,
        "min_ms": round(min(values), 3) if values else 0.0,
    }


def _seed_ops(game_uuid: str, count: int):
    ops, observations = [], []
    for i in range(1, count + 1):
        key = f"rk-perf-{i}"
        ops.append({
            "op_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"perf-seed:{i}")),
            "game_uuid": game_uuid, "device_id": "perf", "device_seq": i,
            "lamport": i, "kind": "review_award",
            "payload": {"review_key": key, "review_ts": 1785542400 + i,
                        "rating": 3, "review_kind": "review",
                        "provenance": "direct", "skill": "mining",
                        "resource": "Rune essence"}})
        observations.append({"review_key": key, "revlog_id": 2000000 + i,
                             "card_id": 3000000 + i, "fingerprint": key})
    return ops, observations


def _wait_for_revision(worker, revision: int, timeout: float = 60.0) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        latest = worker.latest()
        if latest is not None and int(latest.get("revision", -1)) >= revision:
            return True
        time.sleep(0.005)
    return False


def measure_answer_hook(rules, game_uuid: str, history: int, samples: int,
                        workdir: str):
    """Time real accepted answers through the full durable pipeline: the
    main-thread hook (atomic journal write, no replay) with the projection
    worker attached, plus durable-append-to-published reward completion."""
    journal = Journal(os.path.join(workdir, f"hook-{history}.sqlite3"))
    worker = None
    try:
        ops, observations = _seed_ops(game_uuid, history)
        if ops:
            journal.import_game(game_uuid, {"operations": ops,
                                            "observations": observations})
        cfg = EngineConfig(game_uuid=game_uuid, device_id="perf",
                           activated_at=0, rules=rules)
        engine = EvolvedEngine(cfg, journal)
        engine.hydrate()
        worker = ProjectionWorker(journal.path, cfg).start()
        engine.attach_worker(worker)
        worker.notify_dirty()
        if not _wait_for_revision(worker, history, timeout=120.0):
            raise AssertionError("worker never published the seeded projection")
        engine.projection()  # warm the published-state read path

        hook_times = []
        awarded = 0
        busy_retries = 0
        base_revlog = 5000000
        for i in range(samples):
            revlog_id = base_revlog + i
            result = None
            dt = 0.0
            for _attempt in range(40):
                t0 = time.perf_counter()
                result = engine.credit_direct(
                    revlog_id=revlog_id, card_id=base_revlog + i, ease=3,
                    revlog_type=1, review_ts=1785542400 + history + i,
                    skill="mining", resource="Rune essence", reward_policy=2)
                dt = (time.perf_counter() - t0) * 1000.0
                if result.get("ok") or not result.get("needs_recovery"):
                    break
                # Rejected by the deliberate 25 ms fail-fast busy budget,
                # not by slow work: retry outside the measured sample.
                busy_retries += 1
                time.sleep(0.05)
            hook_times.append(dt)
            if not result.get("ok"):
                raise AssertionError(f"credit failed: {result}")
        expected = history + samples
        completion_ms = None
        t0 = time.perf_counter()
        if _wait_for_revision(worker, expected, timeout=120.0):
            completion_ms = (time.perf_counter() - t0) * 1000.0
        # Per-review durable-append -> displayed-state latency: one review at
        # a time, awaiting its outcome (coalesced publishes are real).
        reward_times = []
        rounds = 30
        for i in range(rounds):
            revlog_id = base_revlog + samples + i
            result = None
            t0 = time.perf_counter()
            for _attempt in range(40):
                result = engine.credit_direct(
                    revlog_id=revlog_id, card_id=base_revlog + samples + i,
                    ease=3, revlog_type=1,
                    review_ts=1785542400 + history + samples + i,
                    skill="mining", resource="Rune essence", reward_policy=2)
                if result.get("ok") or not result.get("needs_recovery"):
                    break
                busy_retries += 1
                time.sleep(0.05)
                t0 = time.perf_counter()
            if not result.get("ok"):
                raise AssertionError(f"credit failed: {result}")
            key = result["review_key"]
            deadline = time.perf_counter() + 30.0
            while engine.outcome_for(key) is None and time.perf_counter() < deadline:
                time.sleep(0.005)
            if engine.outcome_for(key) is None:
                raise AssertionError("reward outcome never published")
            reward_times.append((time.perf_counter() - t0) * 1000.0)
            if (engine.outcome_for(key) or {}).get("rewarded"):
                awarded += 1
        final_revision = history + samples + rounds
        if not _wait_for_revision(worker, final_revision, timeout=120.0):
            raise AssertionError("worker did not reach the final revision")
        full = replay(journal.all_operations(), rules, game_uuid)
        incremental = engine.projection()
        keys = ("xp_micro", "levels", "inventory", "achievements", "revision",
                "total_level", "counters")
        equivalent = all(incremental.get(k) == full.get(k) for k in keys)
        return {"hook": _summary_ms(hook_times),
                "reward": _summary_ms(reward_times),
                "batch_completion_ms": round(completion_ms or -1, 1),
                "awarded": awarded, "busy_retries": busy_retries,
                "equivalence": equivalent}
    finally:
        if worker is not None:
            try:
                worker.stop(timeout=5.0)
            except Exception:
                pass
        journal.close()


def measure_rebuild(rules, game_uuid: str, history: int = 100000,
                    repetitions: int = 3):
    ops, _obs = _seed_ops(game_uuid, history)
    ops.append({"op_id": str(uuid.uuid4()), "game_uuid": game_uuid,
                "device_id": "perf", "device_seq": history + 1,
                "lamport": history + 1, "kind": "review_retract",
                "payload": {"target_review_key": "rk-perf-1",
                            "reason": "anki_undo"}})
    times = []
    state = None
    for _ in range(repetitions):
        t0 = time.perf_counter()
        state = replay(ops, rules, game_uuid)
        times.append((time.perf_counter() - t0) * 1000.0)
    assert state is not None
    return {"runs_ms": [round(t, 1) for t in times],
            "max_ms": round(max(times), 1),
            "p50_ms": round(nearest_rank_percentile(times, 50), 1),
            "samples": len(times)}


def _menu_model_samples(rules, repetitions: int, state=None):
    levels = (state or {}).get("levels") or {
        s: 50 for s in ("mining", "woodcutting", "smithing", "crafting",
                        "fishing", "cooking")}
    xp = (state or {}).get("xp_micro") or {s: 50_000 * 1_000_000 for s in levels}
    inv = (state or {}).get("inventory") or {"Rune essence": 320}
    sel = {"mining": "Runite ore", "woodcutting": "Redwood",
           "smithing": "Rune bar", "crafting": "Gold ring",
           "fishing": "Shark", "cooking": "Shark"}
    samples = []
    for _ in range(repetitions):
        t0 = time.perf_counter()
        rows = skill_rows(rules, levels, xp, inv, sel)
        assert len(rows) == 6
        achievement_rows(rules.get("achievements", {}).get("required", []), [])
        hiscores_status(logged_in=True, last_success="today", pending=3)
        samples.append((time.perf_counter() - t0) * 1000.0)
    return samples


def measure_shell(rules, samples: int, cold_ms: float):
    warm = _menu_model_samples(rules, samples)
    return _summary_ms(warm), {"ms": round(cold_ms, 1)}


def measure_cold_shell(rules, game_uuid: str, history: int, workdir: str):
    """Cold shell appearance proxy: opening a loaded game must return a
    usable state (persisted projection or explicit loading state) fast, with
    no main-thread history replay. The worker builds and checkpoints history
    outside the timed region first, as a real prior session would."""
    path = os.path.join(workdir, "cold-shell.sqlite3")
    journal = Journal(path)
    worker = None
    worker2 = None
    try:
        ops, observations = _seed_ops(game_uuid, history)
        journal.import_game(game_uuid, {"operations": ops, "observations": observations})
        cfg = EngineConfig(game_uuid=game_uuid, device_id="perf",
                           activated_at=0, rules=rules)
        worker = ProjectionWorker(path, cfg, checkpoint_every=1,
                                  checkpoint_interval=0.0).start()
        worker.notify_dirty()
        if not _wait_for_revision(worker, history, timeout=120.0):
            raise AssertionError("worker never published the seeded projection")
        worker.flush_checkpoint()
        worker.stop(timeout=10.0)
        worker = None
        journal.close()

        cold_journal = Journal(path)
        cold_engine = EvolvedEngine(cfg, cold_journal)
        worker2 = ProjectionWorker(path, cfg).start()
        cold_engine.attach_worker(worker2)
        t0 = time.perf_counter()
        projection = cold_engine.projection()
        usable = bool(projection) and ("loading" in projection or projection.get("levels"))
        if usable and not projection.get("loading"):
            _menu_model_samples(rules, 1, projection)
        elif usable:
            _menu_model_samples(rules, 1)
        dt = (time.perf_counter() - t0) * 1000.0
        return dt
    finally:
        for item in (worker, worker2):
            if item is not None:
                try:
                    item.stop(timeout=5.0)
                except Exception:
                    pass
        try:
            journal.close()
        except Exception:
            pass


def measure_endurance(rules, game_uuid: str, minutes: float, workdir: str):
    """RSS slope approximation with fixed-size history (pure, small scale)."""
    journal = Journal(os.path.join(workdir, "endurance.sqlite3"))
    try:
        ops, observations = _seed_ops(game_uuid, 1000)
        journal.import_game(game_uuid, {"operations": ops, "observations": observations})
        cfg = EngineConfig(game_uuid=game_uuid, device_id="perf",
                           activated_at=0, rules=rules)
        engine = EvolvedEngine(cfg, journal)
        engine.hydrate()
        engine.projection()
        deadline = time.time() + max(1.0, minutes) * 60
        samples, i = [], 0
        while time.time() < deadline:
            engine.credit_direct(revlog_id=7000000 + i, card_id=7000000 + i,
                                 ease=3, revlog_type=1,
                                 review_ts=1785542400 + 1000 + i,
                                 skill="mining", resource="Rune essence",
                                 reward_policy=2)
            i += 1
            if i % 20 == 0:
                rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                if sys.platform == "darwin":
                    rss_mib = rss / (1024 * 1024)
                else:
                    rss_mib = rss / 1024
                samples.append((time.time(), rss_mib))
        if len(samples) >= 4:
            warm = samples[len(samples) // 3:]
            (t0, m0), (t1, m1) = warm[0], warm[-1]
            slope = (m1 - m0) / max(0.001, (t1 - t0) / 60.0)
            settled = max(s[1] for s in warm) - min(s[1] for s in warm)
        else:
            slope, settled = 0.0, 0.0
        return {"slope_mib_per_min": round(slope, 3),
                "settled_increase_mib": round(settled, 1),
                "samples": len(samples), "answers": i}
    finally:
        journal.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="perf_runtime.py")
    parser.add_argument("--profile", default="nightly",
                        choices=sorted(PROFILES))
    parser.add_argument("--json", default="")
    parser.add_argument("--samples", type=int, default=0,
                        help="override per-size sample count (0 = profile)")
    parser.add_argument("--endurance-minutes", type=float, default=0.0)
    parser.add_argument("--no-gate", action="store_true")
    args = parser.parse_args(argv)

    rules = load_rules()
    game_uuid = "perf-" + str(uuid.uuid4())
    workdir = tempfile.mkdtemp(prefix="ankiscape-perf-")
    started = time.time()
    metrics = {"accepted_answer_hook": {}, "reward_completion": {},
               "warm_shell_open": None, "cold_shell_appearance": None}
    equivalence = {}
    hook = {}
    try:
        for history in HISTORY_SIZES:
            samples = args.samples or (PROFILES[args.profile]["large"] if history >= 100000
                                       else PROFILES[args.profile]["small"])
            result = measure_answer_hook(rules, game_uuid, history, samples, workdir)
            hook[str(history)] = result["hook"]
            metrics["accepted_answer_hook"][str(history)] = result["hook"]
            if history:
                metrics["reward_completion"] = result["reward"]
            equivalence[str(history)] = result["equivalence"]
        small = hook.get("1000", {}).get("p95_ms")
        large = hook.get("100000", {}).get("p95_ms")
        metrics["warm_review_scaling"] = {
            "p95_ratio_100000_over_1000":
                round((large / small), 3) if small and large else None,
            "p95_1000_ms": small, "p95_100000_ms": large}
        metrics["late_retraction_rebuild"] = measure_rebuild(rules, game_uuid)
        metrics["warm_shell_open"] = _summary_ms(
            _menu_model_samples(rules, 50))
        metrics["cold_shell_appearance"] = {
            "ms": round(measure_cold_shell(rules, game_uuid, 100000, workdir), 1)}
        if args.endurance_minutes > 0:
            metrics["endurance_memory"] = measure_endurance(
                rules, game_uuid, args.endurance_minutes, workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    failures = _evaluate(metrics, args.profile, args.no_gate)
    system = {"os": sys.platform, "python": sys.version.split()[0]}
    try:
        import platform

        system.update({"os": platform.system().lower(), "arch": platform.machine()})
    except Exception:
        pass
    out = {"schema": "ankiscape-perf-runtime", "version": 1,
           "profile": args.profile, "started_at": int(started),
           "duration_s": round(time.time() - started, 1),
           "game_uuid": game_uuid, "params": {"sizes": list(HISTORY_SIZES)},
           "system": system, "metrics": metrics, "equivalence": equivalence,
           "budget_failures": failures,
           "pass": not failures}
    text = json.dumps(out, indent=2)
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
    for failure in failures:
        print(f"perf_runtime: FAIL {failure}", file=sys.stderr)
    return 0 if not failures else 1


def _evaluate(metrics, profile: str, no_gate: bool):
    if profile == "baseline" or no_gate:
        return []
    failures = []
    hook = metrics.get("accepted_answer_hook") or {}
    cfg = BUDGETS["accepted_answer_hook"]
    enforce = profile == "release"
    for size in cfg["history_sizes"]:
        entry = hook.get(str(size)) or {}
        if enforce and entry.get("samples", 0) < cfg["min_samples"]:
            failures.append(f"accepted_answer_hook:samples:{size}:"
                            f"{entry.get('samples')}")
        if entry.get("p95_ms", 1e9) > cfg["limit"]:
            failures.append(f"accepted_answer_hook:p95:{size}:{entry.get('p95_ms')}")
        if entry.get("p99_ms", 1e9) > cfg["p99_limit"]:
            failures.append(f"accepted_answer_hook:p99:{size}:{entry.get('p99_ms')}")
    ratio = (metrics.get("warm_review_scaling") or {}).get(
        "p95_ratio_100000_over_1000")
    if ratio is None or ratio > BUDGETS["warm_review_scaling"]["limit"]:
        failures.append(f"warm_review_scaling:ratio:{ratio}")
    reward = metrics.get("reward_completion") or {}
    if enforce and reward.get("samples", 0) < BUDGETS["reward_completion"]["min_samples"]:
        failures.append(f"reward_completion:samples:{reward.get('samples')}")
    if reward.get("p95_ms", 1e9) > BUDGETS["reward_completion"]["limit"]:
        failures.append(f"reward_completion:p95:{reward.get('p95_ms')}")
    rebuild = metrics.get("late_retraction_rebuild") or {}
    if rebuild.get("max_ms", 1e9) > BUDGETS["late_retraction_rebuild"]["hard_limit"]:
        failures.append(f"late_retraction_rebuild:max:{rebuild.get('max_ms')}")
    elif rebuild.get("max_ms", 1e9) > BUDGETS["late_retraction_rebuild"]["target"]:
        failures.append(f"late_retraction_rebuild:target_missed:{rebuild.get('max_ms')}")
    shell = metrics.get("warm_shell_open") or {}
    if shell.get("p95_ms", 1e9) > BUDGETS["warm_shell_open"]["limit"]:
        failures.append(f"warm_shell_open:p95:{shell.get('p95_ms')}")
    if not all(metrics.get("equivalence", {}).values()):
        failures.append("state_equivalence:false")
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
