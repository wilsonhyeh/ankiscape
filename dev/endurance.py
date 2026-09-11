#!/usr/bin/env python3
# dev/endurance.py - Active endurance run with memory/leak bounds (dev only).
"""Runs the real durable review pipeline (journal + worker + projection) at a
synthetic reviewer pace against a fixed-size history, recording RSS slope,
object/thread counts and answer throughput:

  python3 dev/endurance.py --minutes 30
  python3 dev/endurance.py --minutes 120 --answers-per-second 2

Budgets (see dev/reliability-budgets.json): after warm-up the final-segment
RSS slope must be <=1 MiB/min and the settled increase <=50 MiB over the warm
baseline with fixed-size history. Native UI cycles (open/close, profile/mode
switches, offline/reconnect) run in the platform lane journeys; this script
covers the durability/projection layers and records which layers ran.

Exit 0 when the measured pure layers pass; nonzero otherwise.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import resource
import sys
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evolved.data import load_rules  # noqa: E402
from evolved.engine import EngineConfig, EvolvedEngine  # noqa: E402
from evolved.journal import Journal  # noqa: E402
from evolved.projection_worker import ProjectionWorker  # noqa: E402
from evolved.reducer import replay  # noqa: E402

SLOPE_LIMIT_MIB_PER_MIN = 1.0
SETTLED_LIMIT_MIB = 50.0
FIXED_HISTORY = 1000


def _metrics_module():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "endurance_metrics.py")
    spec = importlib.util.spec_from_file_location(
        "ankiscape_endurance_metrics", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rss_mib() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return value / (1024 * 1024)
    return value / 1024


def _seed(game: str, journal: Journal, count: int) -> List[Dict[str, Any]]:
    ops = []
    for i in range(1, count + 1):
        key = f"rk-end-{i}"
        ops.append({"op_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"end:{i}")),
                    "game_uuid": game, "device_id": "endurance",
                    "device_seq": i, "lamport": i, "kind": "review_award",
                    "payload": {"review_key": key, "review_ts": 2000 + i,
                                "rating": 3, "review_kind": "review",
                                "provenance": "direct", "reward_policy": 2,
                                "skill": "mining", "resource": "Rune essence"}})
    journal.import_game(game, {"operations": ops, "observations": []})
    return ops


def run(minutes: float, answers_per_second: float,
        json_path: str = "") -> int:
    rules = load_rules()
    game = "endurance-" + str(uuid.uuid4())
    tmp = tempfile.TemporaryDirectory(prefix="ankiscape-endurance-")
    samples: List[Dict[str, Any]] = []
    answers = 0
    busy_retries = 0
    start = time.monotonic()
    journal = Journal(os.path.join(tmp.name, "game.sqlite3"))
    worker = None
    try:
        _seed(game, journal, FIXED_HISTORY)
        cfg = EngineConfig(game_uuid=game, device_id="endurance",
                           activated_at=0, rules=rules)
        engine = EvolvedEngine(cfg, journal)
        engine.hydrate()
        worker = ProjectionWorker(journal.path, cfg).start()
        engine.attach_worker(worker)
        worker.notify_dirty()
        deadline_wait = time.monotonic() + 30
        while worker.latest() is None and time.monotonic() < deadline_wait:
            time.sleep(0.01)
        interval = 1.0 / max(0.1, answers_per_second)
        deadline = time.monotonic() + max(0.05, minutes) * 60
        next_sample = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            t0 = time.monotonic()
            revlog = 9000000 + answers
            result = None
            for _attempt in range(40):
                result = engine.credit_direct(
                    revlog_id=revlog, card_id=revlog, ease=3, revlog_type=1,
                    review_ts=1900000000 + answers, skill="mining",
                    resource="Rune essence", reward_policy=2)
                if result.get("ok") or not result.get("needs_recovery"):
                    break
                busy_retries += 1
                time.sleep(0.05)
            if not result.get("ok"):
                raise RuntimeError(f"credit failed: {result}")
            answers += 1
            now = time.monotonic()
            if now >= next_sample:
                next_sample = now + 30.0
                samples.append({
                    "at_s": round(now - start, 1), "rss_mib": round(_rss_mib(), 1),
                    "objects": len(gc.get_objects()),
                    "threads": threading.active_count(),
                    "answers": answers,
                    "worker_revision": int(
                        (worker.latest() or {}).get("revision", 0))})
            sleep = interval - (time.monotonic() - t0)
            if sleep > 0:
                time.sleep(sleep)
        # Final equivalence check: worker state equals full replay (wait for
        # the coalesced publish to reach the newest revision first).
        expected = journal.operation_count()
        wait_deadline = time.monotonic() + 120
        while time.monotonic() < wait_deadline:
            latest = worker.latest() or {}
            if int(latest.get("revision", 0)) >= expected:
                break
            time.sleep(0.01)
        if not worker.latest():
            raise RuntimeError("worker never published")
        reference = replay(journal.all_operations(), rules, game)
        latest = engine.projection()
        equivalent = all(latest.get(k) == reference.get(k)
                         for k in ("xp_micro", "inventory", "levels", "revision"))
    finally:
        if worker is not None:
            worker.stop(timeout=5.0)
        journal.close()
        tmp.cleanup()

    report = _metrics_module().evaluate_endurance(
        samples, profile="engine-smoke", duration_min=minutes)
    failures = list(report.get("failures") or [])
    if not equivalent:
        failures.append("final worker state != reference replay")
    summary = {
        "minutes": minutes, "answers_per_second": answers_per_second,
        "answers": answers, "busy_retries": busy_retries,
        "fixed_history": FIXED_HISTORY,
        "layers": ["journal", "engine", "projection_worker"],
        "native_ui_cycles": "platform lane journeys (ui-lifecycle)",
        "eligible_for_release": False,
        "slope_mib_per_min": report.get("slope_mib_per_min"),
        "settled_increase_mib": report.get("settled_increase_mib"),
        "samples": samples, "equivalence": equivalent,
        "budgets": {"slope_mib_per_min": SLOPE_LIMIT_MIB_PER_MIN,
                    "settled_increase_mib": SETTLED_LIMIT_MIB},
        "pass": not failures, "failures": failures,
        "finished_at": int(time.time()),
    }
    if json_path:
        os.makedirs(os.path.dirname(os.path.abspath(json_path)), exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
    print(json.dumps({k: summary[k] for k in (
        "minutes", "answers", "slope_mib_per_min", "settled_increase_mib",
        "equivalence", "pass", "failures")}, indent=2))
    return 0 if not failures else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="endurance.py")
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--answers-per-second", type=float, default=2.0)
    parser.add_argument("--json", default="")
    args = parser.parse_args(argv)
    return run(args.minutes, args.answers_per_second, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
