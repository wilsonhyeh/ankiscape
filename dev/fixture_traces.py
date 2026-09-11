#!/usr/bin/env python3
# dev/fixture_traces.py - Deterministic hosted fixture traces (dev only).
"""Builds the hosted-v1 fixture operation traces for the 24 permanent test
players. Everything is deterministic: op ids (uuid5), device sequence,
lamport and payloads derive only from the suite id/version and the player's
slot, so reruns adopt the same identities and replay the exact same
operations; changing a trace requires a new fixture version.

Trace coverage: zero scores, ties, mixed skills, material pauses, recipe
success/failure, mined gems, Undo/retract/restore and deterministic final
standings. Credentials are never generated or stored here (see
dev/seed_hosted_fixtures.py).
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import uuid
from typing import Any, Dict, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fractions import Fraction  # noqa: E402

from evolved.data import load_rules  # noqa: E402
from evolved.draws import draw_r, frac_hits  # noqa: E402
from evolved.journal import canonical_json  # noqa: E402
from evolved.logic_pure import gathering_probability  # noqa: E402
from evolved.reducer import _gem_pick, replay  # noqa: E402

SUITE_ID = "hosted-v1"
SUITE_VERSION = 1
DEVICE_ID = "fixture-v1"
MAX_OPS_PER_PLAYER = 3000
MAX_TOTAL_OPS = 24000
_GEM_KEY_CACHE: Dict[str, str] = {}


def gem_key_for(game: str, rules: Dict[str, Any]) -> str:
    """A deterministic review key whose Clay award rolls a mined gem on this
    game identity (searched once; the same game always resolves the same key)."""
    cached = _GEM_KEY_CACHE.get(game)
    if cached:
        return cached
    clay = next(o for o in rules["ores"] if o["display"] == "Clay")
    prob = gathering_probability(1, clay["probability"])
    for i in range(200000):
        key = f"rk-gem-{game}-{i}"
        if not frac_hits(draw_r(1, game, key, "action"), prob):
            continue
        if not frac_hits(draw_r(1, game, key, "gem_drop"), Fraction(1, 256)):
            continue
        if _gem_pick(draw_r(1, game, key, "gem_pick"), rules["gems"]) is not None:
            _GEM_KEY_CACHE[game] = key
            return key
    raise RuntimeError(f"no deterministic gem key found for {game}")


def fixture_uuid(kind: str, name: str, suite: str = SUITE_ID,
                 version: int = SUITE_VERSION) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"ankiscape:{kind}:{suite}:v{version}:{name}"))


def email_for(name: str, suite: str = SUITE_ID) -> str:
    norm = name.strip().lower()
    return f"{norm}@{suite}.example.invalid"


def display_name(name: str) -> str:
    return str(name).strip()


def username_norm(name: str) -> str:
    return str(name).strip().lower()


def _op(index: int, game: str, kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "op_id": str(uuid.uuid5(uuid.NAMESPACE_URL,
                                f"ankiscape:op:{SUITE_ID}:v{SUITE_VERSION}:"
                                f"{game}:{index}")),
        "game_uuid": game, "device_id": DEVICE_ID,
        "device_seq": index, "lamport": index, "kind": kind,
        "payload": payload,
    }


def _award(index: int, game: str, key: str, skill: str, resource: str,
           *, ease: int = 3, policy: int = 2, ts: int = 1800000000) -> Dict:
    return _op(index, game, "review_award", {
        "review_key": key, "review_ts": ts + index, "rating": ease,
        "review_kind": "review", "provenance": "direct",
        "reward_policy": policy, "skill": skill, "resource": resource})


def _skip(index: int, game: str, key: str, reason: str = "classic_mode") -> Dict:
    return _op(index, game, "review_skip", {
        "review_key": key, "review_ts": 1800000000 + index,
        "reason": reason, "provenance": "skip"})


def _retract(index: int, game: str, key: str, *, restore: bool = False) -> Dict:
    return _op(index, game, "review_retract" if not restore else "review_restore", {
        "target_review_key": key, "reason": "anki_redo" if restore else "anki_undo"})


def _mixed_script(rng: random.Random, game: str, count: int) -> List[Dict]:
    """A deterministic mixed-skill script with pauses and failures."""
    ops: List[Dict] = []
    ores = [o["display"] for o in sorted(load_rules().get("ores", []),
                                         key=lambda o: int(o["tier"]))]
    trees = [t["display"] for t in sorted(load_rules().get("trees", []),
                                          key=lambda t: int(t["tier"]))]
    fish = [f["display"] for f in sorted(load_rules().get("fish", []),
                                         key=lambda f: int(f["tier"]))]
    index = 0
    for i in range(count):
        roll = rng.random()
        index += 1
        key = f"rk-{game}-{i}"
        if roll < 0.34:
            ops.append(_award(index, game, key, "mining",
                              ores[rng.randrange(min(4, len(ores)))]))
        elif roll < 0.52:
            ops.append(_award(index, game, key, "woodcutting",
                              trees[rng.randrange(min(3, len(trees)))]))
        elif roll < 0.68:
            ops.append(_award(index, game, key, "fishing",
                              fish[rng.randrange(min(3, len(fish)))]))
        elif roll < 0.78:
            ops.append(_award(index, game, key, "cooking",
                              fish[rng.randrange(len(fish))]))
        elif roll < 0.88:
            ops.append(_award(index, game, key, "smithing", "Bronze bar"))
        elif roll < 0.94:
            ops.append(_award(index, game, key, "crafting", "Gold ring"))
        elif roll < 0.97:
            ops.append(_skip(index, game, key))
        else:
            if ops:
                target = str(ops[-1]["payload"].get("review_key")
                             or ops[-1]["payload"].get("target_review_key"))
                ops.append(_retract(index, game, target))
                index += 1
                ops.append(_retract(index, game, target, restore=True))
    return ops


def _trace_for(name: str, slot: int) -> List[Dict]:
    game = fixture_uuid("game", name)
    if slot == 0:
        # Zero scores: skips and policy-2 level-blocked recipes only.
        ops = [_skip(i, game, f"rk-{game}-skip-{i}") for i in range(1, 31)]
        for i in range(31, 51):
            ops.append(_award(i, game, f"rk-{game}-locked-{i}", "cooking",
                              "Trout", policy=2))
        return ops
    if slot in (1, 2):
        # Ties: identical deterministic script for both players. Policy-1
        # cooking Trout is level-blocked practice XP (exactly 1 XP per review,
        # no draws), so both finish with identical XP and shared ranks.
        return [_award(i, game, f"rk-{game}-tie-{i}", "cooking", "Trout",
                       policy=1) for i in range(1, 121)]
    if slot == 3:
        # Recipe chain: gathering feeds smithing/crafting; failures included.
        ops = []
        index = 0
        for i in range(1, 61):
            index += 1
            ops.append(_award(index, game, f"rk-{game}-ore-{i}", "mining", "Copper ore"))
            index += 1
            ops.append(_award(index, game, f"rk-{game}-tin-{i}", "mining", "Tin ore"))
            index += 1
            ops.append(_award(index, game, f"rk-{game}-bar-{i}", "smithing", "Bronze bar"))
        return ops
    if slot == 4:
        # Material pauses + cooking gate: no fish for the cooks, one fish later.
        ops = [_award(i, game, f"rk-{game}-pause-{i}", "cooking", "Trout",
                      policy=2) for i in range(1, 41)]
        ops.append(_award(41, game, f"rk-{game}-fish", "fishing", "Shrimp"))
        ops.append(_award(42, game, f"rk-{game}-cook", "cooking", "Shrimp",
                          policy=2))
        ops.append(_award(43, game, f"rk-{game}-cook2", "cooking", "Shrimp",
                          policy=2))
        return ops
    if slot == 5:
        # Mined gems: one deterministic drop key plus ordinary clay mining.
        rules = load_rules()
        ops = [_award(1, game, gem_key_for(game, rules), "mining", "Clay",
                      policy=2)]
        ops += [_award(i, game, f"rk-{game}-gem-{i}", "mining", "Clay")
                for i in range(2, 42)]
        return ops
    if slot == 6:
        # Undo/retract/restore: one restored award, one left retracted.
        ops = [_award(1, game, f"rk-{game}-u1", "mining", "Rune essence"),
               _award(2, game, f"rk-{game}-u2", "woodcutting", "Tree"),
               _retract(3, game, f"rk-{game}-u1"),
               _retract(4, game, f"rk-{game}-u2"),
               _retract(5, game, f"rk-{game}-u2", restore=True)]
        return ops
    rng = random.Random(f"{SUITE_ID}:v{SUITE_VERSION}:{username_norm(name)}")
    return _mixed_script(rng, game, 120)


def build_traces(names: List[str]) -> Dict[str, Dict[str, Any]]:
    """{display_name: {game_uuid, email, ops, trace_hash, expected}}."""
    rules = load_rules()
    out: Dict[str, Dict[str, Any]] = {}
    total = 0
    for slot, name in enumerate(names):
        ops = _trace_for(name, slot)
        if len(ops) > MAX_OPS_PER_PLAYER:
            raise ValueError(f"{name}: {len(ops)} ops exceeds per-player bound")
        total += len(ops)
        if total > MAX_TOTAL_OPS:
            raise ValueError("fixture v1 exceeds the total operation bound")
        game = fixture_uuid("game", name)
        state = replay(ops, rules, game)
        trace_hash = hashlib.sha256(canonical_json(ops)).hexdigest()
        out[display_name(name)] = {
            "slot": slot, "username_norm": username_norm(name),
            "display": display_name(name), "email": email_for(name),
            "game_uuid": game, "ops": ops, "trace_hash": trace_hash,
            "expected": {
                "xp_micro": state["xp_micro"],
                "levels": state["levels"],
                "total_level": state["total_level"],
                "inventory": state["inventory"],
                "achievements": state["achievements"],
                "revision": state["revision"],
            }}
    return out


def load_suite(path: str = "") -> Dict[str, Any]:
    path = path or os.path.join(ROOT, "dev", "fixtures", "hosted-v1.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def suite_summary() -> Dict[str, Any]:
    suite = load_suite()
    traces = build_traces(suite["display_names"])
    total = sum(len(t["ops"]) for t in traces.values())
    return {
        "suite_id": SUITE_ID, "suite_version": SUITE_VERSION,
        "players": len(traces), "total_ops": total,
        "max_player_ops": max(len(t["ops"]) for t in traces.values()),
        "batch_size": suite["trace"]["batch_size"],
        "traces": {name: {"trace_hash": t["trace_hash"],
                          "ops": len(t["ops"]),
                          "game_uuid": t["game_uuid"],
                          "total_level": t["expected"]["total_level"]}
                   for name, t in sorted(traces.items())},
    }


if __name__ == "__main__":
    print(json.dumps(suite_summary(), indent=2))
