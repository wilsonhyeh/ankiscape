#!/usr/bin/env python3
# dev/demo_traces.py - Deterministic traces for the five public demo players.
"""Builds the public-demo-v1 operation histories for the five permanent,
publicly visible players: DemoWillow, DemoFlint, DemoMoss, DemoRowan and
DemoCopper.

Everything is deterministic: op ids (uuid5), device sequences, lamports and
payloads derive only from the suite id/version and the demo name. Histories
are legitimate replayable operations (gathering, cooking, smelting and
crafting chains); no direct XP or state edits. Generation fails instead of
silently changing goals when the coverage or operation bounds cannot be met.

Coverage guarantee: every demo has nonzero XP in all six skills, the
population has varied scores, and DemoFlint and DemoMoss share a reproducible
tied cooking score (equal policy-1 practice awards, which carry no draws).

Credentials are never generated or stored here (see dev/demo_players.py).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from typing import Any, Dict, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evolved.data import load_rules  # noqa: E402
from evolved.journal import canonical_json  # noqa: E402
from evolved.reducer import replay  # noqa: E402

SUITE_ID = "public-demo"
SUITE_VERSION = 1
DEVICE_ID = "demo-v1"
MAX_OPS_PER_DEMO = 3000
MAX_TOTAL_OPS = 15000
PUBLIC_LABEL = "Demo"

DISPLAY_NAMES = ("DemoWillow", "DemoFlint", "DemoMoss", "DemoRowan",
                 "DemoCopper")

# Per-demo operation plans. Each entry is (skill, resource) repeated `count`
# times in the listed order; the chains include their prerequisites so replay
# never depends on luck for coverage (extra attempts cover failed gathers).
_PLANS: Dict[str, List[Tuple[str, str, int]]] = {
    "DemoWillow": [
        ("mining", "Rune essence", 6), ("woodcutting", "Tree", 8),
        ("fishing", "Shrimp", 8), ("cooking", "Shrimp", 5),
        ("mining", "Clay", 8), ("crafting", "Soft clay", 4),
        ("crafting", "Unfired pot", 2), ("crafting", "Pot", 2),
        ("mining", "Copper ore", 6), ("mining", "Tin ore", 6),
        ("smithing", "Bronze bar", 4),
    ],
    "DemoFlint": [
        ("mining", "Rune essence", 3), ("woodcutting", "Tree", 4),
        ("fishing", "Shrimp", 3),
        ("mining", "Clay", 3), ("crafting", "Soft clay", 1),
        ("mining", "Copper ore", 3), ("mining", "Tin ore", 3),
        ("smithing", "Bronze bar", 1),
    ],
    "DemoMoss": [
        ("mining", "Rune essence", 6), ("woodcutting", "Tree", 2),
        ("fishing", "Shrimp", 6),
        ("mining", "Clay", 6), ("crafting", "Soft clay", 2),
        ("mining", "Copper ore", 4), ("mining", "Tin ore", 4),
        ("smithing", "Bronze bar", 2),
    ],
    "DemoRowan": [
        ("mining", "Rune essence", 10), ("woodcutting", "Tree", 3),
        ("fishing", "Shrimp", 3), ("cooking", "Shrimp", 1),
        ("mining", "Clay", 4), ("crafting", "Soft clay", 1),
        ("crafting", "Unfired pot", 1), ("crafting", "Pot", 1),
        ("mining", "Copper ore", 10), ("mining", "Tin ore", 10),
        ("smithing", "Bronze bar", 6),
    ],
    "DemoCopper": [
        ("mining", "Rune essence", 3), ("woodcutting", "Tree", 3),
        ("fishing", "Shrimp", 12), ("cooking", "Shrimp", 8),
        ("mining", "Clay", 3), ("crafting", "Soft clay", 1),
        ("mining", "Copper ore", 3), ("mining", "Tin ore", 3),
        ("smithing", "Bronze bar", 1),
    ],
}

# Equal policy-1 practice awards give DemoFlint and DemoMoss the exact same
# cooking score (1 XP each, no draws), producing a reproducible tie.
_TIE_COOKING_PRACTICE = {"DemoFlint": 20, "DemoMoss": 20}
TIE_SKILL = "cooking"
TIE_PAIR = ("DemoFlint", "DemoMoss")


def demo_uuid(kind: str, name: str, suite: str = SUITE_ID,
              version: int = SUITE_VERSION) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"ankiscape:{kind}:{suite}:v{version}:{name}"))


def email_for(name: str, suite: str = SUITE_ID) -> str:
    return f"{name.strip().lower()}@{suite}.example.invalid"


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
           *, ease: int = 3, policy: int = 2) -> Dict:
    return _op(index, game, "review_award", {
        "review_key": key, "review_ts": 1800000000 + index, "rating": ease,
        "review_kind": "review", "provenance": "direct",
        "reward_policy": policy, "skill": skill, "resource": resource})


def _trace_for(name: str) -> List[Dict]:
    name = display_name(name)
    if name not in _PLANS:
        raise ValueError(f"no demo plan for {name!r}")
    game = demo_uuid("game", name)
    ops: List[Dict] = []
    index = 0
    for skill, resource, count in _PLANS[name]:
        for _ in range(int(count)):
            index += 1
            ops.append(_award(index, game, f"rk-{name}-{index}", skill,
                              resource))
    for _ in range(int(_TIE_COOKING_PRACTICE.get(name, 0))):
        index += 1
        ops.append(_award(index, game, f"rk-{name}-tie-{index}", "cooking",
                          "Trout", policy=1))
    return ops


def _coverage_errors(name: str, state: Dict[str, Any]) -> List[str]:
    errors = []
    xp = state.get("xp_micro", {}) or {}
    for skill in ("mining", "woodcutting", "smithing", "crafting",
                  "fishing", "cooking"):
        if int(xp.get(skill, 0) or 0) <= 0:
            errors.append(f"{name}: zero {skill} XP")
    return errors


def build_traces(names: Tuple[str, ...] = DISPLAY_NAMES) -> Dict[str, Dict[str, Any]]:
    """{display: {game_uuid, email, ops, trace_hash, expected}}."""
    rules = load_rules()
    out: Dict[str, Dict[str, Any]] = {}
    total = 0
    for name in names:
        display = display_name(name)
        ops = _trace_for(display)
        if len(ops) > MAX_OPS_PER_DEMO:
            raise ValueError(f"{display}: {len(ops)} ops exceeds the "
                             f"{MAX_OPS_PER_DEMO}-op per-demo bound")
        total += len(ops)
        if total > MAX_TOTAL_OPS:
            raise ValueError(f"demo suite exceeds the {MAX_TOTAL_OPS}-op "
                             "total bound; generation goals cannot be met")
        game = demo_uuid("game", display)
        state = replay(ops, rules, game)
        errors = _coverage_errors(display, state)
        if errors:
            raise ValueError("demo coverage failed: " + "; ".join(errors))
        trace_hash = hashlib.sha256(canonical_json(ops)).hexdigest()
        out[display] = {
            "username_norm": username_norm(display),
            "display": display,
            "email": email_for(display),
            "game_uuid": game,
            "ops": ops,
            "trace_hash": trace_hash,
            "expected": {
                "xp_micro": state["xp_micro"],
                "levels": state["levels"],
                "total_level": state["total_level"],
                "inventory": state["inventory"],
                "achievements": state["achievements"],
                "revision": state["revision"],
            },
        }
    _assert_tie(out)
    return out


def _assert_tie(traces: Dict[str, Dict[str, Any]]) -> None:
    left, right = TIE_PAIR
    if left not in traces or right not in traces:
        raise ValueError("tie pair missing from the demo population")
    left_xp = int((traces[left]["expected"]["xp_micro"] or {}).get(TIE_SKILL, 0))
    right_xp = int((traces[right]["expected"]["xp_micro"] or {}).get(TIE_SKILL, 0))
    if left_xp <= 0 or left_xp != right_xp:
        raise ValueError(
            f"expected a reproducible {TIE_SKILL} tie between {left} and "
            f"{right}; got {left_xp} vs {right_xp}")


def manifest_digest(traces: Dict[str, Dict[str, Any]]) -> str:
    """Checksum over the exact identity/trace/expected payloads."""
    payload = {name: {"username_norm": t["username_norm"],
                      "email": t["email"], "game_uuid": t["game_uuid"],
                      "trace_hash": t["trace_hash"],
                      "expected": t["expected"]}
               for name, t in sorted(traces.items())}
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def load_suite(path: str = "") -> Dict[str, Any]:
    path = path or os.path.join(ROOT, "dev", "fixtures", "public-demo-v1.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        # Regeneration and tests may run before the checked-in manifest
        # exists; the deterministic defaults stand in.
        return {"suite_id": SUITE_ID, "suite_version": SUITE_VERSION,
                "display_names": list(DISPLAY_NAMES),
                "trace": {"batch_size": 200}}


def suite_summary() -> Dict[str, Any]:
    suite = load_suite()
    names = tuple(suite.get("display_names") or DISPLAY_NAMES)
    traces = build_traces(names)
    total = sum(len(t["ops"]) for t in traces.values())
    return {
        "suite_id": SUITE_ID, "suite_version": SUITE_VERSION,
        "label": PUBLIC_LABEL,
        "players": len(traces), "total_ops": total,
        "max_demo_ops": max(len(t["ops"]) for t in traces.values()),
        "batch_size": int((suite.get("trace") or {}).get("batch_size", 200)),
        "manifest_digest": manifest_digest(traces),
        "traces": {name: {"trace_hash": t["trace_hash"],
                          "ops": len(t["ops"]),
                          "game_uuid": t["game_uuid"],
                          "xp_micro": t["expected"]["xp_micro"],
                          "total_level": t["expected"]["total_level"]}
                   for name, t in sorted(traces.items())},
    }


def build_fixture() -> Dict[str, Any]:
    """The checked-in public-demo-v1 manifest with exact expectations."""
    traces = build_traces(DISPLAY_NAMES)
    return {
        "suite_id": SUITE_ID,
        "suite_version": SUITE_VERSION,
        "label": PUBLIC_LABEL,
        "note": ("Permanent public demo players. Deterministic legitimate "
                 "operation histories replayed through the authoritative "
                 "submission RPCs; no direct state edits. Reserved "
                 ".example.invalid emails. No credentials or scores are "
                 "stored in this file; expectations are derived by "
                 "dev/demo_traces.py and checked by dev/demo_players.py."),
        "display_names": list(DISPLAY_NAMES),
        "trace": {
            "version": 1,
            "device_id": DEVICE_ID,
            "max_ops_per_demo": MAX_OPS_PER_DEMO,
            "max_total_ops": MAX_TOTAL_OPS,
            "batch_size": 200,
            "bounds": {
                "one_run_at_a_time": True,
                "max_requests_per_second": 2,
                "max_concurrent_requests": 2,
                "request_timeout_s": 30,
                "max_retries": 3,
                "abort_on_429_or_5xx_after": 3,
            },
        },
        "tie": {"skill": TIE_SKILL, "pair": list(TIE_PAIR)},
        "manifest_digest": manifest_digest(traces),
        "players": {
            name: {
                "username_norm": t["username_norm"],
                "display": t["display"],
                "email": t["email"],
                "game_uuid": t["game_uuid"],
                "ops": len(t["ops"]),
                "trace_hash": t["trace_hash"],
                "expected": t["expected"],
            }
            for name, t in sorted(traces.items())
        },
    }


def write_fixture(path: str = "") -> str:
    path = path or os.path.join(ROOT, "dev", "fixtures", "public-demo-v1.json")
    data = build_fixture()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


def main(argv: List[str]) -> int:
    if "--write-fixture" in argv:
        path = write_fixture()
        print(f"demo_traces: wrote {os.path.relpath(path, ROOT)}")
        return 0
    if "--json" in argv:
        print(json.dumps(suite_summary(), indent=2, sort_keys=True))
        return 0
    summary = suite_summary()
    print(f"demo suite {summary['suite_id']} v{summary['suite_version']}: "
          f"{summary['players']} players, {summary['total_ops']} ops "
          f"(max {summary['max_demo_ops']}/demo)")
    for name, info in summary["traces"].items():
        print(f"  {name:<12} ops={info['ops']:<4} "
              f"total_level={info['total_level']:<3} hash={info['trace_hash'][:16]}")
    print(f"manifest_digest={summary['manifest_digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
