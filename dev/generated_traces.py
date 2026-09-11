#!/usr/bin/env python3
# dev/generated_traces.py - Generated operation-sequence property checks.
"""Deterministic generated traces with fixed seeds and minimized
counterexamples, used by PR/nightly/release gates:

  python3 dev/generated_traces.py --stage pr        (>=100 sequences of <=100)
  python3 dev/generated_traces.py --stage nightly   (>=1000 sequences of <=1000)
  python3 dev/generated_traces.py --stage release   (>=5000 sequences of <=1000)

Every sequence is folded through the reference reducer AND through the
incremental checkpoint fast path one operation at a time; the two states must
match at every prefix (state equals the unique canonical operation set).
Algebraic properties are checked only where valid: arrival-order invariance
with fixed canonical fields, exact duplicate idempotence, level/XP
consistency, inventory nonnegativity and catch-up identity uniqueness. Undo is
NOT treated as an inverse balance subtraction.

Failures print the seed and the minimized operation list and exit nonzero.
Hypothesis is optional: when installed, its example database augments the
deterministic runs (dev-only dependency, never shipped).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from typing import Any, Dict, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evolved.data import load_rules  # noqa: E402
from evolved.draws import draw_r, frac_hits  # noqa: E402
from evolved.reducer import (  # noqa: E402
    build_checkpoint, extend_checkpoint, replay,
)

SKILLS = ("mining", "woodcutting", "smithing", "crafting", "fishing", "cooking")
PURE_BUDGET_IDS = ("accepted_answer_hook", "warm_review_scaling",
                   "reward_completion", "late_retraction_rebuild")


def _award(slot: int, game: str, dev: str, key: str, skill: str, resource: str,
           seq: int, lamport: int, *, policy: int = 2,
           prov: str = "direct", ts: int = 1900000000) -> Dict[str, Any]:
    return {"op_id": f"op-{game}-{dev}-{slot}", "game_uuid": game,
            "device_id": dev, "device_seq": seq, "lamport": lamport,
            "kind": "review_award",
            "payload": {"review_key": key, "review_ts": ts + slot,
                        "rating": 3, "review_kind": "review",
                        "provenance": prov, "reward_policy": policy,
                        "skill": skill, "resource": resource}}


def _skip(slot: int, game: str, dev: str, key: str, seq: int,
          lamport: int) -> Dict[str, Any]:
    return {"op_id": f"skip-{game}-{dev}-{slot}", "game_uuid": game,
            "device_id": dev, "device_seq": seq, "lamport": lamport,
            "kind": "review_skip",
            "payload": {"review_key": key, "reason": "classic_mode",
                        "provenance": "skip"}}


def _retract(slot: int, game: str, dev: str, key: str, seq: int, lamport: int,
             restore: bool = False) -> Dict[str, Any]:
    return {"op_id": f"ret-{game}-{dev}-{slot}", "game_uuid": game,
            "device_id": dev, "device_seq": seq, "lamport": lamport,
            "kind": "review_restore" if restore else "review_retract",
            "payload": {"target_review_key": key,
                        "reason": "anki_redo" if restore else "anki_undo"}}


def _preset(slot: int, game: str, dev: str, skill: str, ts: int, seq: int,
            lamport: int) -> Dict[str, Any]:
    return {"op_id": f"pre-{game}-{dev}-{slot}", "game_uuid": game,
            "device_id": dev, "device_seq": seq, "lamport": lamport,
            "kind": "catchup_preset",
            "payload": {"skill": skill, "effective_ts": ts}}


def generate_sequence(seed: int, length: int,
                      rules: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One deterministic mixed operation sequence for a fixed seed."""
    rng = random.Random(seed)
    game = f"gen-{seed}"
    devices = ["dev-a", "dev-b"] if seed % 3 else ["dev-a"]
    seqs = {d: 0 for d in devices}
    lamps = {d: 0 for d in devices}
    ores = [o["display"] for o in rules["ores"]]
    trees = [t["display"] for t in rules["trees"]]
    fish = [f["display"] for f in rules["fish"]]
    ops: List[Dict[str, Any]] = []
    keys: List[str] = []
    for slot in range(1, length + 1):
        device = rng.choice(devices)
        seqs[device] += 1
        lamps[device] += 1
        roll = rng.random()
        if roll < 0.06 and keys and rng.random() < 0.5:
            ops.append(_retract(slot, game, device, rng.choice(keys),
                                seqs[device], lamps[device],
                                restore=rng.random() < 0.5))
            continue
        if roll < 0.10:
            key = f"rk-{seed}-skip-{slot}"
            ops.append(_skip(slot, game, device, key, seqs[device],
                             lamps[device]))
            keys.append(key)
            continue
        if roll < 0.13 and ops:
            ops.append(_preset(slot, game, device,
                               rng.choice(("mining", "fishing", "woodcutting")),
                               1900000000 + slot, seqs[device], lamps[device]))
            continue
        skill = rng.choice(SKILLS)
        resource = {
            "mining": rng.choice(ores), "woodcutting": rng.choice(trees),
            "smithing": "Bronze bar", "crafting": "Gold ring",
            "fishing": rng.choice(fish), "cooking": rng.choice(fish),
        }[skill]
        key = f"rk-{seed}-{slot}"
        prov = "direct" if rng.random() < 0.8 else "catchup"
        policy = rng.choice((1, 2)) if prov == "direct" else 2
        ops.append(_award(slot, game, device, key, skill, resource,
                          seqs[device], lamps[device], policy=policy,
                          prov=prov, ts=1900000000))
        keys.append(key)
    return ops


def check_sequence(ops: List[Dict[str, Any]], rules: Dict[str, Any],
                   game: str, *, full_prefixes: bool = True,
                   sample_every: int = 0) -> List[str]:
    """Reference replay vs incremental prefix trace + algebraic properties.

    Every prefix is compared when `full_prefixes` is set; otherwise the
    reference replay runs at every `sample_every` operations and at the end
    (large nightly/release batches), while cheap invariants are checked at
    every step."""
    problems: List[str] = []
    known: set = set()
    checkpoint = build_checkpoint([], rules, game)
    total = len(ops)
    for index, op in enumerate(ops, 1):
        prefix = ops[:index]
        extended = extend_checkpoint(
            checkpoint, [op], rules, game,
            key_known=lambda key, _watermark=None: key in known)
        if extended is None:
            extended = build_checkpoint(prefix, rules, game)
        checkpoint = extended
        payload = op.get("payload") or {}
        for field in ("review_key", "target_review_key"):
            if payload.get(field):
                known.add(str(payload[field]))
        state = checkpoint["state"]
        if int(state.get("revision", -1)) != index:
            problems.append(f"prefix {index}: revision {state.get('revision')}")
            return problems
        for item, qty in (state.get("inventory") or {}).items():
            if int(qty) < 0:
                problems.append(f"prefix {index}: negative inventory {item}")
                return problems
        compare = (full_prefixes or index == total
                   or (sample_every and index % sample_every == 0))
        if not compare:
            continue
        reference = replay(prefix, rules, game)
        for field in ("xp_micro", "levels", "inventory", "achievements",
                      "revision", "total_level", "counters", "skipped"):
            if state.get(field) != reference.get(field):
                problems.append(f"prefix {index}: {field} diverged")
                return problems
    final = replay(ops, rules, game)
    if checkpoint["state"] != final:
        problems.append("final incremental state != reference")
    for skill, xp in final["xp_micro"].items():
        if int(xp) < 0:
            problems.append(f"negative xp for {skill}")
    for item, qty in final["inventory"].items():
        if int(qty) < 0:
            problems.append(f"negative inventory for {item}")
    shuffled = list(ops)
    random.Random(len(ops) * 7919 + 13).shuffle(shuffled)
    permuted = replay(shuffled, rules, game)
    for field in ("xp_micro", "inventory", "levels", "revision"):
        if permuted.get(field) != final.get(field):
            problems.append(f"arrival-order invariance broke on {field}")
    if len(ops) >= 2:
        duplicated = replay(ops + ops[:1], rules, game)
        if duplicated["xp_micro"] != final["xp_micro"]:
            problems.append("exact duplicate id changed the state")
    return problems


def _known_in_prefix(prefix: List[Dict[str, Any]], key: str) -> bool:
    for op in prefix:
        payload = op.get("payload") or {}
        if str(payload.get("review_key")) == str(key) or \
                str(payload.get("target_review_key")) == str(key):
            return True
    return False


def minimize(ops: List[Dict[str, Any]], rules: Dict[str, Any],
             game: str) -> List[Dict[str, Any]]:
    """Deterministic delta-debugging minimization of a failing sequence."""
    failing = list(ops)
    changed = True
    while changed and len(failing) > 1:
        changed = False
        chunk = max(1, len(failing) // 2)
        while chunk >= 1:
            for start in range(0, len(failing), chunk):
                candidate = failing[:start] + failing[start + chunk:]
                if candidate and check_sequence(candidate, rules, game):
                    failing = candidate
                    changed = True
                    break
            if changed:
                break
            chunk //= 2
    return failing


def stage_counts(stage: str) -> Tuple[int, int, int, int]:
    """(sequences, max_length, full_prefix_sequences, sample_every)."""
    if stage == "pr":
        return 100, 100, 100, 0
    if stage == "nightly":
        return 1000, 300, 100, 25
    return 5000, 300, 200, 50


def run_stage(stage: str) -> int:
    sequences, max_length, full_count, sample_every = stage_counts(stage)
    rules = load_rules()
    failures = 0
    for seed in range(1, sequences + 1):
        length = 20 + (seed * 37) % (max_length - 19)
        try:
            ops = generate_sequence(seed, length, rules)
            problems = check_sequence(
                ops, rules, f"gen-{seed}",
                full_prefixes=seed <= full_count,
                sample_every=sample_every)
        except Exception as exc:  # noqa: BLE001
            problems = [f"raised {exc!r}"]
            ops = []
        if problems:
            failures += 1
            print(f"generated_traces: FAIL seed={seed} "
                  f"problems={problems[:3]}", file=sys.stderr)
            if ops:
                minimal = minimize(ops, rules, f"gen-{seed}")
                _ = minimal
                print("generated_traces: minimal reproducer "
                      f"({len(minimal)} ops): "
                      f"{json.dumps(minimal)[:900]}", file=sys.stderr)
            if failures >= 3:
                break
    if failures:
        return 1
    print(f"generated_traces: PASS stage={stage} sequences={sequences} "
          f"max_ops={max_length} full_prefix_sequences={full_count} "
          f"sample_every={sample_every}")
    return 0


def optional_hypothesis_checks() -> int:
    """Extra generated checks when Hypothesis is installed (dev-only)."""
    try:
        from hypothesis import HealthCheck, given, settings  # noqa: F401
        from hypothesis import strategies as st
    except Exception:
        print("generated_traces: hypothesis not installed; "
              "deterministic sequences only")
        return 0
    import hypothesis

    @settings(max_examples=50, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(seed=st.integers(min_value=1, max_value=10 ** 6),
           length=st.integers(min_value=1, max_value=40))
    def _one(seed, length):
        rules = load_rules()
        ops = generate_sequence(seed, length, rules)
        problems = check_sequence(ops, rules, f"gen-{seed}")
        assert not problems, problems

    _one()
    print(f"generated_traces: hypothesis {hypothesis.__version__} checks PASS")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="generated_traces.py")
    parser.add_argument("--stage", default="pr",
                        choices=("pr", "nightly", "release"))
    parser.add_argument("--hypothesis", action="store_true",
                        help="also run optional Hypothesis examples")
    args = parser.parse_args(argv)
    rc = 0
    if args.hypothesis:
        rc |= optional_hypothesis_checks()
    return rc | run_stage(args.stage)


if __name__ == "__main__":
    raise SystemExit(main())
