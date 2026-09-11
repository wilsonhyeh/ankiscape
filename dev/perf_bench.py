#!/usr/bin/env python3
# dev/perf_bench.py - Local perf bars (dev only, never shipped).
"""Measures the plan's task-10 performance bars on THIS machine:

  - menu model build p95 over 20 warm runs (<300 ms bar)
  - 200-operation replay/commit p95 (<2 s bar)
  - 100k-operation late-retraction rebuild (<10 s HTTP deadline; <8 s target)

Prints a machine-readable JSON summary + human lines. Exits nonzero when a
bar fails. No Anki/Qt needed (pure model + journal + reducer); the menu bar
covers model construction, which dominates menu build (Qt shell is thin).
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from evolved.data import load_rules  # noqa: E402
from evolved.journal import Journal  # noqa: E402
from evolved.reducer import replay  # noqa: E402
from evolved.ui.menu_model import (achievement_rows, hiscores_status,  # noqa: E402
                                   skill_rows)

BARS = {"menu_model_p95_ms": 300.0, "replay200_p95_ms": 2000.0,
        "rebuild100k_ms": 10000.0, "rebuild100k_target_ms": 8000.0}


def _p95(samples):
    """Nearest-rank p95: sorted x[ceil(0.95 * n)], no interpolation.

    The returned value is always an observed sample, so it can never exceed
    the observed maximum (the old statistics.quantiles approach could with
    small n). Requires an adequate sample count; callers below use >=100.
    """
    xs = sorted(float(s) for s in samples)
    rank = max(1, math.ceil(0.95 * len(xs)))
    return xs[min(rank, len(xs)) - 1]


def _menu_model_bar(rules):
    levels = {s: 50 for s in ("mining", "woodcutting", "smithing", "crafting",
                              "fishing", "cooking")}
    xp = {s: 50_000 * 1_000_000 for s in levels}
    inv = {"Rune essence": 320, "Trout": 40, "cooked_Trout": 12}
    sel = {"mining": "Runite ore", "woodcutting": "Redwood",
           "smithing": "Rune bar", "crafting": "Gold ring",
           "fishing": "Shark", "cooking": "Shark"}
    samples = []
    for _ in range(100):
        t0 = time.perf_counter()
        rows = skill_rows(rules, levels, xp, inv, sel)
        assert len(rows) == 6
        achievement_rows(rules.get("achievements", {}).get("required", []), [])
        hiscores_status(logged_in=True, last_success="today", pending=3)
        samples.append((time.perf_counter() - t0) * 1000)
    return samples


def _replay200_bar(rules, game_uuid):
    tmp = tempfile.TemporaryDirectory()
    journal = Journal(os.path.join(tmp.name, "bench.sqlite3"))
    ops = []
    for i in range(1, 201):
        op = {"op_id": str(uuid.uuid4()), "game_uuid": game_uuid,
              "device_id": "bench", "device_seq": i, "lamport": i,
              "kind": "review_award",
              "payload": {"review_key": f"rk-bench-{i}",
                          "review_ts": 1785542400 + i, "rating": 3,
                          "review_kind": "review", "provenance": "direct",
                          "skill": "mining", "resource": "Rune essence"}}
        journal.append_operation(op)
        ops.append(op)
    samples = []
    for _ in range(100):
        t0 = time.perf_counter()
        state = replay(ops, rules, game_uuid)
        samples.append((time.perf_counter() - t0) * 1000)
    assert state["total_level"] > 0
    journal.close()
    tmp.cleanup()
    return samples


def _rebuild100k_bar(rules, game_uuid):
    ops = []
    for i in range(1, 100001):
        ops.append({"op_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"bench100k-{i}")),
                    "game_uuid": game_uuid, "device_id": "bench",
                    "device_seq": i, "lamport": i, "kind": "review_award",
                    "payload": {"review_key": f"rk-100k-{i}",
                                "review_ts": 1785542400 + i, "rating": 3,
                                "review_kind": "review", "provenance": "direct",
                                "skill": "mining", "resource": "Rune essence"}})
    # Late retraction of the FIRST award (worst case: invalidates everything).
    ops.append({"op_id": str(uuid.uuid4()), "game_uuid": game_uuid,
                "device_id": "bench", "device_seq": 100001, "lamport": 100001,
                "kind": "review_retract",
                "payload": {"target_review_key": "rk-100k-1",
                            "reason": "anki_undo"}})
    t0 = time.perf_counter()
    state = replay(ops, rules, game_uuid)
    dt_ms = (time.perf_counter() - t0) * 1000
    assert state["total_level"] >= 0
    return dt_ms


def main():
    rules = load_rules()
    game_uuid = str(uuid.uuid4())
    menu = _menu_model_bar(rules)
    rep200 = _replay200_bar(rules, game_uuid)
    big = _rebuild100k_bar(rules, game_uuid)
    summary = {
        "menu_model_p95_ms": round(_p95(menu), 2),
        "menu_model_max_ms": round(max(menu), 2),
        "replay200_p95_ms": round(_p95(rep200), 2),
        "replay200_max_ms": round(max(rep200), 2),
        "rebuild100k_ms": round(big, 1),
        "bars": BARS,
        "pass": (_p95(menu) < BARS["menu_model_p95_ms"]
                 and _p95(rep200) < BARS["replay200_p95_ms"]
                 and big < BARS["rebuild100k_ms"]),
        "target_8s": big < BARS["rebuild100k_target_ms"],
    }
    print(json.dumps(summary, indent=2))
    for key in ("menu_model_p95_ms", "replay200_p95_ms", "rebuild100k_ms"):
        bar = {"menu_model_p95_ms": BARS["menu_model_p95_ms"],
               "replay200_p95_ms": BARS["replay200_p95_ms"],
               "rebuild100k_ms": BARS["rebuild100k_ms"]}[key]
        print(f"{'PASS' if summary[key] < bar else 'FAIL'} {key}={summary[key]} bar={bar}")
    if not summary["target_8s"]:
        print(f"NOTE rebuild100k {big:.0f} ms exceeds the 8 s target "
              f"(still within the 10 s deadline)")
    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
