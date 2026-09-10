# evolved/reducer.py - Deterministic replay in canonical order (contract C).
"""Replays active awards in canonical order (lamport, device_id, device_seq,
op_id), independent of network arrival. Python and Postgres implement the same
specified rules + golden vectors. Each action checks reconstructed level and
inventory before consuming. Never sums per-device balances.

Reward-policy versions (per payload):
  1 (historical; absent marker) - missing materials or an unmet level award a
     small practice XP and produce nothing.
  2 (all new operations) - an invalid recipe awards ZERO XP, consumes
     nothing, and produces nothing; still recorded so catch-up cannot
     re-credit it later.

Claim precedence per review_key: direct > skip (Classic-mode no-reward claim)
> catch-up. A retract disables all claims until an explicit restore.
"""
from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple

from .draws import draw_r, frac_hits
from .logic_pure import (
    MICRO, SKILLS, burn_probability, eval_cook, eval_gather, eval_production,
    gathering_probability, level_from_xp_micro,
)

CATCHUP_SKILLS = ("mining", "woodcutting", "fishing")
_DIRECT_SKILLS = ("mining", "woodcutting", "fishing", "cooking", "smithing", "crafting")
SUPPORTED_POLICIES = (1, 2)


def canonical_order_key(op: Dict[str, Any]):
    return (int(op["lamport"]), str(op["device_id"]), int(op["device_seq"]), str(op["op_id"]))


def reward_policy_of(payload: Dict[str, Any]) -> int:
    """Absent marker means historical policy 1; unknown values are returned
    as-is so replay can reject them explicitly."""
    value = (payload or {}).get("reward_policy", 1)
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _first_unlocked(entries, level: int):
    unlocked = [e for e in entries if level >= int(e.get("level", e.get("fishing_level", 1)))]
    if not unlocked:
        return entries[0]
    return sorted(unlocked, key=lambda e: int(e["tier"]))[0]


def _highest_unlocked(entries, level: int, level_key: str = "level"):
    unlocked = [e for e in entries if level >= int(e.get(level_key, 1))]
    if not unlocked:
        return None
    return sorted(unlocked, key=lambda e: int(e["tier"]))[-1]


def _gem_pick(r: int, gems) -> Dict | None:
    # Cumulative legacy distribution over r/2^48: 1/4, 1/8, 1/16, 1/64.
    bounds = []
    cum = Fraction(0, 1)
    ordered = sorted(gems, key=lambda g: int(g.get("order", 0)))
    for g in ordered:
        cum += Fraction(int(g["probability_num"]), int(g["probability_den"]))
        bounds.append((cum, g))
    pos = Fraction(r, 1 << 48)
    for cum_bound, g in bounds:
        if pos < cum_bound:
            return g
    return None


def replay(operations: List[Dict[str, Any]], rules: Dict[str, Any],
           game_uuid: str) -> Dict[str, Any]:
    """Replay operation set to a revisioned state. Pure + deterministic.

    Returns {xp_micro, levels, inventory, achievements, counters,
    revision, adjustments, diagnostics, conflicts, review_outcomes, skipped}.
    """
    diagnostics: List[str] = []
    conflicts: List[Dict[str, Any]] = []

    # Dedup by op_id; changed payload with reused id is a conflict.
    by_id: Dict[str, Dict] = {}
    for op in operations:
        oid = str(op.get("op_id", ""))
        if not oid:
            conflicts.append({"type": "missing_op_id", "op": op})
            continue
        if oid in by_id:
            if by_id[oid] != op:
                conflicts.append({"type": "reused_id_changed_content", "op_id": oid})
            continue
        by_id[oid] = op

    # (device_id, seq) reuse with different payload is preserved, not overwritten.
    seen_seq: Dict[Tuple[str, int], Dict] = {}
    ops: List[Dict] = []
    for op in by_id.values():
        try:
            key = (str(op["device_id"]), int(op["device_seq"]))
            if int(op["device_seq"]) <= 0:
                raise ValueError("device_seq must be positive")
        except (KeyError, ValueError, TypeError):
            conflicts.append({"type": "bad_sequence", "op_id": op.get("op_id")})
            continue
        if key in seen_seq and seen_seq[key].get("payload") != op.get("payload"):
            conflicts.append({"type": "seq_reuse_changed_payload", "device": key[0], "seq": key[1]})
            continue
        seen_seq.setdefault(key, op)
        ops.append(op)

    ops.sort(key=canonical_order_key)

    # Preset timeline (effective_ts, canonical_pos, skill).
    presets: List[Tuple[int, int, str]] = []
    for pos, op in enumerate(ops):
        if op.get("kind") == "catchup_preset":
            payload = op.get("payload", {}) or {}
            skill = str(payload.get("skill", "")).lower()
            if skill not in CATCHUP_SKILLS:
                conflicts.append({"type": "bad_preset_skill", "op_id": op.get("op_id")})
                continue
            try:
                ts = int(payload.get("effective_ts", 0))
            except (ValueError, TypeError):
                conflicts.append({"type": "bad_preset_ts", "op_id": op.get("op_id")})
                continue
            presets.append((ts, pos, skill))
    presets.sort()

    def preset_for(review_ts: int) -> str:
        skill = "mining"  # default before first preset
        for ts, _, s in presets:
            if ts <= review_ts:
                skill = s
            else:
                break
        return skill

    # Group review awards + retractions + skips by review_key.
    awards_by_key: Dict[str, List[Tuple[int, Dict]]] = {}
    retract_by_key: Dict[str, List[Tuple[int, Dict]]] = {}
    skip_by_key: Dict[str, Tuple[int, Dict]] = {}
    for pos, op in enumerate(ops):
        kind = op.get("kind")
        payload = op.get("payload", {}) or {}
        if kind == "review_award":
            rk = str(payload.get("review_key", ""))
            if not rk:
                conflicts.append({"type": "missing_review_key", "op_id": op.get("op_id")})
                continue
            policy = reward_policy_of(payload)
            if policy not in SUPPORTED_POLICIES:
                diagnostics.append(f"unsupported_policy:{rk}:{policy}")
                continue
            awards_by_key.setdefault(rk, []).append((pos, op))
        elif kind in ("review_retract", "review_restore"):
            rk = str(payload.get("target_review_key", ""))
            if not rk:
                conflicts.append({"type": "missing_target_key", "op_id": op.get("op_id")})
                continue
            retract_by_key.setdefault(rk, []).append((pos, op))
        elif kind == "review_skip":
            rk = str(payload.get("review_key", ""))
            if not rk:
                conflicts.append({"type": "missing_review_key", "op_id": op.get("op_id")})
                continue
            existing = skip_by_key.get(rk)
            if existing is None or pos < existing[0]:
                skip_by_key[rk] = (pos, op)

    # Winning claim per key: direct beats skip beats catch-up; earliest
    # canonical within a provenance. Retraction: last retract/restore decides.
    winners: List[Tuple[int, str, Dict]] = []
    for rk, lst in awards_by_key.items():
        # Last retract/restore in canonical order decides.
        retracts = sorted(retract_by_key.get(rk, []), key=lambda t: t[0])
        if retracts:
            last_kind = retracts[-1][1].get("kind")
            if last_kind == "review_retract":
                continue  # disabled until explicit redo restore
        directs = [(p, o) for p, o in lst if (o.get("payload", {}) or {}).get("provenance") == "direct"]
        catches = [(p, o) for p, o in lst if (o.get("payload", {}) or {}).get("provenance") != "direct"]
        if directs:
            pos, op = sorted(directs, key=lambda t: t[0])[0]
            if len(directs) > 1:
                diagnostics.append(f"duplicate_direct_ignored:{rk}")
            winners.append((pos, rk, op))
        elif rk in skip_by_key:
            # Classic-mode no-reward claim suppresses catch-up credit.
            diagnostics.append(f"claim_skipped:{rk}")
        elif catches:
            pos, op = sorted(catches, key=lambda t: t[0])[0]
            if len(catches) > 1:
                diagnostics.append(f"duplicate_catchup_ignored:{rk}")
            winners.append((pos, rk, op))
    winners.sort(key=lambda t: t[0])

    xp: Dict[str, int] = {s: 0 for s in SKILLS}
    inv: Dict[str, int] = {}
    counters: Dict[str, int] = {"successful_actions": 0, "cooking_attempts": 0,
                                "successful_cooks": 0, "gems": 0,
                                "first_catch": 0, "first_cook": 0}
    per_skill_success: Dict[str, int] = {s: 0 for s in SKILLS}
    adjustments: List[str] = []
    review_outcomes: Dict[str, Dict[str, Any]] = {}
    rules_version = int(rules.get("rules_version", 1))
    thresholds = rules.get("thresholds", [])

    ores = {o["display"]: o for o in rules.get("ores", [])}
    trees = {t["display"]: t for t in rules.get("trees", [])}
    fish = {f["display"]: f for f in rules.get("fish", [])}
    bars = {b["display"]: b for b in rules.get("bars", [])}
    crafts = {c["display"]: c for c in rules.get("crafting", [])}
    gems = rules.get("gems", [])

    def add_item(name: str, qty: int):
        inv[name] = inv.get(name, 0) + qty
        if inv[name] < 0:
            raise AssertionError("negative inventory - replay bug")

    for pos, rk, op in winners:
        payload = op.get("payload", {}) or {}
        prov = payload.get("provenance", "catchup")
        policy = reward_policy_of(payload)
        levels = {s: level_from_xp_micro(xp[s], thresholds) for s in SKILLS}
        if prov == "direct":
            skill = str(payload.get("skill", "")).lower()
            resource = str(payload.get("resource", ""))
            if skill not in _DIRECT_SKILLS:
                diagnostics.append(f"unknown_skill:{rk}")
                outcomes = {"skill": skill or "mining", "xp_micro": 0,
                            "outcome": "invalid", "rewarded": False,
                            "items": {}, "consumed": {}}
                if policy != 2:
                    xp["mining"] += MICRO
                    outcomes["xp_micro"] = MICRO
                    outcomes["outcome"] = "practice"
                review_outcomes[rk] = outcomes
                continue
            res = _apply_direct(skill, resource, levels, inv, rules, rules_version,
                                game_uuid, rk, gems, ores, trees, fish, bars, crafts,
                                diagnostics, reward_policy=policy)
        else:
            try:
                review_ts = int(payload.get("review_ts", 0))
            except (ValueError, TypeError):
                review_ts = 0
            skill = preset_for(review_ts)
            res = _apply_catchup(skill, levels, inv, rules, rules_version, game_uuid,
                                 rk, gems, ores, trees, fish, diagnostics,
                                 reward_policy=policy)
        # Apply result to state.
        if res is None:
            continue
        consumed = res.get("consumed", {})
        conflicted = False
        for name, qty in consumed.items():
            if inv.get(name, 0) < qty:
                # Stale provisional outcome: shared-ingredient conflict. The
                # stable replay gives the item to the first canonical claim.
                adjustments.append(f"material_conflict:{rk}:{name}")
                if policy == 2:
                    res = {"skill": res["skill"], "xp_micro": 0, "consumed": {},
                           "counters": {}, "outcome": "paused_materials"}
                    review_outcomes[rk] = {
                        "skill": res["skill"], "xp_micro": 0,
                        "outcome": "paused_materials", "rewarded": False,
                        "items": {}, "consumed": {},
                        "adjustment": f"material_conflict:{name}"}
                else:
                    xp[res["skill"]] += MICRO
                    res = {"skill": res["skill"], "xp_micro": MICRO,
                           "consumed": {}, "counters": {}, "outcome": "practice"}
                    review_outcomes[rk] = {
                        "skill": res["skill"], "xp_micro": MICRO,
                        "outcome": "practice", "rewarded": True,
                        "items": {}, "consumed": {},
                        "adjustment": f"material_conflict:{name}"}
                conflicted = True
                break
        if res is None:
            continue
        if not conflicted:
            xp[res["skill"]] += res["xp_micro"]
            for name, qty in res.get("consumed", {}).items():
                inv[name] = inv.get(name, 0) - qty
            items = {}
            if res.get("item_out"):
                add_item(res["item_out"], res.get("item_qty", 1))
                items[res["item_out"]] = res.get("item_qty", 1)
            if res.get("gem_out"):
                # Gems are extra loot alongside the ore (guide: cut them
                # through Crafting); the XP was already added by _apply_direct.
                add_item(res["gem_out"], 1)
                items[res["gem_out"]] = 1
            review_outcomes[rk] = {
                "skill": res["skill"], "xp_micro": int(res.get("xp_micro", 0)),
                "outcome": res.get("outcome", ""),
                "rewarded": bool(int(res.get("xp_micro", 0)) > 0),
                "items": items, "consumed": dict(res.get("consumed", {})),
                "adjustment": ""}
        counters["successful_actions"] += res.get("counters", {}).get("successful_actions", 0)
        per_skill_success[res["skill"]] += res.get("counters", {}).get("successful_actions", 0)
        for key in ("cooking_attempts", "successful_cooks", "gems"):
            counters[key] += res.get("counters", {}).get(key, 0)
        if res.get("outcome") in ("success",) and res["skill"] == "fishing" and not counters["first_catch"]:
            counters["first_catch"] = 1
        if res.get("outcome") == "success" and res["skill"] == "cooking" and not counters["first_cook"]:
            counters["first_cook"] = 1

    levels = {s: level_from_xp_micro(xp[s], thresholds) for s in SKILLS}
    achievements = _achievements(levels, counters)
    return {
        "xp_micro": dict(xp),
        "xp_display": {s: str(v) for s, v in xp.items()},
        "levels": levels,
        "total_level": sum(levels.values()),
        "inventory": dict(inv),
        "counters": dict(counters),
        "per_skill_success": per_skill_success,
        "achievements": sorted(achievements),
        "revision": len(ops),
        "adjustments": adjustments,
        "diagnostics": diagnostics,
        "conflicts": conflicts,
        "review_outcomes": review_outcomes,
        "skipped": sorted(skip_by_key.keys()),
    }


def _zero(skill: str, outcome: str, diagnostics=None, reason: str = ""):
    if reason and diagnostics is not None:
        diagnostics.append(reason)
    return {"skill": skill, "xp_micro": 0, "consumed": {}, "counters": {},
            "outcome": outcome}


def _apply_direct(skill, resource, levels, inv, rules, rules_version, game_uuid,
                  rk, gems, ores, trees, fish, bars, crafts, diagnostics,
                  reward_policy: int = 1):
    policy2 = reward_policy == 2
    if skill == "mining":
        spec = ores.get(resource)
        if spec is None:
            if policy2:
                return _zero("mining", "invalid",
                             diagnostics, f"unknown_resource:{rk}:{resource}")
            diagnostics.append(f"unknown_resource:{rk}:{resource}")
            return {"skill": "mining", "xp_micro": MICRO, "consumed": {}, "counters": {},
                    "outcome": "practice"}
        if levels["mining"] < int(spec["level"]):
            if policy2:
                return _zero("mining", "paused_level", diagnostics,
                             f"level_blocked:{rk}:{resource}")
            fb = _first_unlocked(list(ores.values()), levels["mining"])
            diagnostics.append(f"level_fallback:{rk}:{resource}->{fb['display']}")
            spec = fb
        prob = gathering_probability(levels["mining"], spec["probability"])
        r_act = draw_r(rules_version, game_uuid, rk, "action")
        success = frac_hits(r_act, prob)
        gem_spec = None
        if success:
            r_drop = draw_r(rules_version, game_uuid, rk, "gem_drop")
            from fractions import Fraction as _F
            if frac_hits(r_drop, _F(1, 256)):
                r_pick = draw_r(rules_version, game_uuid, rk, "gem_pick")
                gem_spec = _gem_pick(r_pick, gems)
        from .logic_pure import eval_gather as _eg
        gem_arg = {"base_xp": gem_spec["base_xp"]} if gem_spec else None
        res = _eg(skill="mining", resource_display=spec["display"], resource_tier=int(spec["tier"]),
                  base_xp=spec["base_xp"], player_level=levels["mining"], success=success,
                  gem=gem_arg, gem_ore_mult_tier=int(spec["tier"]))
        out = {"skill": "mining", "xp_micro": res.xp_micro, "consumed": dict(res.consumed),
               "counters": dict(res.counters), "outcome": res.outcome}
        if res.item_out:
            out["item_out"] = res.item_out
            out["item_qty"] = res.item_qty
            if gem_spec:
                out["gem_out"] = gem_spec["display"]
        return out
    if skill == "woodcutting":
        spec = trees.get(resource)
        if spec is None:
            if policy2:
                return _zero("woodcutting", "invalid", diagnostics,
                             f"unknown_resource:{rk}:{resource}")
            fb = _first_unlocked(list(trees.values()), levels["woodcutting"])
            diagnostics.append(f"level_fallback:{rk}:{resource}->{fb['display']}")
            spec = fb
        elif levels["woodcutting"] < int(spec["level"]):
            if policy2:
                return _zero("woodcutting", "paused_level", diagnostics,
                             f"level_blocked:{rk}:{resource}")
            fb = _first_unlocked(list(trees.values()), levels["woodcutting"])
            diagnostics.append(f"level_fallback:{rk}:{resource}->{fb['display']}")
            spec = fb
        prob = gathering_probability(levels["woodcutting"], spec["probability"])
        r_act = draw_r(rules_version, game_uuid, rk, "action")
        success = frac_hits(r_act, prob)
        from .logic_pure import eval_gather as _eg
        res = _eg(skill="woodcutting", resource_display=spec["display"],
                  resource_tier=int(spec["tier"]), base_xp=spec["base_xp"],
                  player_level=levels["woodcutting"], success=success)
        out = {"skill": "woodcutting", "xp_micro": res.xp_micro, "consumed": {},
               "counters": dict(res.counters), "outcome": res.outcome}
        if res.item_out:
            out["item_out"] = res.item_out
            out["item_qty"] = 1
        return out
    if skill == "fishing":
        spec = fish.get(resource)
        if spec is None:
            if policy2:
                return _zero("fishing", "invalid", diagnostics,
                             f"unknown_resource:{rk}:{resource}")
            shim = _first_unlocked(
                [{"display": f["display"], "level": f["fishing_level"], "tier": f["tier"],
                  "probability": f["probability"], "base_xp": f["fishing_base_xp"]}
                 for f in fish.values()],
                levels["fishing"])
            diagnostics.append(f"level_fallback:{rk}:{resource}->{shim['display']}")
            spec = fish.get(shim["display"], shim)
        elif levels["fishing"] < int(spec["fishing_level"]):
            if policy2:
                return _zero("fishing", "paused_level", diagnostics,
                             f"level_blocked:{rk}:{resource}")
            shim = _first_unlocked(
                [{"display": f["display"], "level": f["fishing_level"], "tier": f["tier"],
                  "probability": f["probability"], "base_xp": f["fishing_base_xp"]}
                 for f in fish.values()],
                levels["fishing"])
            diagnostics.append(f"level_fallback:{rk}:{resource}->{shim['display']}")
            spec = fish.get(shim["display"], shim)
        # Normalize: spec may be a fish row or a shim; find full row by display.
        full = fish.get(spec["display"], spec)
        base = full.get("fishing_base_xp", full.get("base_xp"))
        prob = gathering_probability(levels["fishing"], full["probability"])
        r_act = draw_r(rules_version, game_uuid, rk, "action")
        success = frac_hits(r_act, prob)
        from .logic_pure import eval_gather as _eg
        res = _eg(skill="fishing", resource_display=full["display"],
                  resource_tier=int(full["tier"]), base_xp=base,
                  player_level=levels["fishing"], success=success)
        out = {"skill": "fishing", "xp_micro": res.xp_micro, "consumed": {},
               "counters": dict(res.counters), "outcome": res.outcome}
        if res.item_out:
            out["item_out"] = res.item_out
            out["item_qty"] = 1
        return out
    if skill == "cooking":
        # resource is a fish display to cook.
        spec = fish.get(resource)
        if spec is None:
            if policy2:
                return _zero("cooking", "invalid", diagnostics,
                             f"unknown_resource:{rk}:{resource}")
            diagnostics.append(f"unknown_resource:{rk}:{resource}")
            return {"skill": "cooking", "xp_micro": MICRO, "consumed": {}, "counters": {},
                    "outcome": "practice"}
        level_ok = levels["cooking"] >= int(spec["cooking_level"])
        if not level_ok:
            if policy2:
                return _zero("cooking", "paused_level", diagnostics,
                             f"level_blocked:{rk}:{resource}")
            # Legacy policy: like other production skills, an unmet level
            # earns practice XP and consumes nothing.
            return {"skill": "cooking", "xp_micro": MICRO, "consumed": {},
                    "counters": {}, "outcome": "practice"}
        has = inv.get(spec["display"], 0) >= 1
        if policy2 and not has:
            return _zero("cooking", "paused_materials", diagnostics,
                         f"materials_blocked:{rk}:{resource}")
        r_burn = draw_r(rules_version, game_uuid, rk, "burn")
        burned = frac_hits(r_burn, burn_probability(levels["cooking"]))
        res = eval_cook(fish_display=spec["display"], fish_tier=int(spec["tier"]),
                        cooking_base_xp=spec["cooking_base_xp"],
                        player_level=levels["cooking"], burned=burned, has_fish=has)
        out = {"skill": "cooking", "xp_micro": res.xp_micro, "consumed": dict(res.consumed),
               "counters": dict(res.counters), "outcome": res.outcome}
        if res.item_out:
            out["item_out"] = res.item_out
            out["item_qty"] = 1
        return out
    if skill in ("smithing", "crafting"):
        table = bars if skill == "smithing" else crafts
        spec = table.get(resource)
        if spec is None:
            if policy2:
                return _zero(skill, "invalid", diagnostics,
                             f"unknown_resource:{rk}:{resource}")
            diagnostics.append(f"unknown_resource:{rk}:{resource}")
            return {"skill": skill, "xp_micro": MICRO, "consumed": {}, "counters": {},
                    "outcome": "practice"}
        req = spec.get("ore_required", spec.get("requirements", {}))
        level_ok = levels[skill] >= int(spec["level"])
        mats_ok = all(inv.get(n, 0) >= q for n, q in req.items())
        if policy2 and not level_ok:
            return _zero(skill, "paused_level", diagnostics,
                         f"level_blocked:{rk}:{resource}")
        if policy2 and not mats_ok:
            return _zero(skill, "paused_materials", diagnostics,
                         f"materials_blocked:{rk}:{resource}")
        res = eval_production(skill=skill, output_display=spec["display"],
                              output_tier=int(spec["tier"]), base_xp=spec["base_xp"],
                              level_ok=level_ok, materials_ok=mats_ok, requirements=req)
        out = {"skill": skill, "xp_micro": res.xp_micro, "consumed": dict(res.consumed),
               "counters": dict(res.counters), "outcome": res.outcome}
        if res.item_out:
            out["item_out"] = res.item_out
            out["item_qty"] = 1
        return out
    diagnostics.append(f"unknown_skill:{rk}:{skill}")
    if policy2:
        return _zero("mining", "invalid")
    return {"skill": "mining", "xp_micro": MICRO, "consumed": {}, "counters": {},
            "outcome": "practice"}


def _apply_catchup(skill, levels, inv, rules, rules_version, game_uuid, rk, gems,
                   ores, trees, fish, diagnostics, reward_policy: int = 1):
    if skill == "mining":
        entries = list(ores.values())
        top = _highest_unlocked(entries, levels["mining"])
        if top is None:
            top = entries[0]
        return _apply_direct("mining", top["display"], levels, inv, rules, rules_version,
                             game_uuid, rk, gems, ores, trees, fish, {}, {}, diagnostics,
                             reward_policy=reward_policy)
    if skill == "woodcutting":
        entries = list(trees.values())
        top = _highest_unlocked(entries, levels["woodcutting"])
        if top is None:
            top = entries[0]
        return _apply_direct("woodcutting", top["display"], levels, inv, rules,
                             rules_version, game_uuid, rk, gems, ores, trees, fish,
                             {}, {}, diagnostics, reward_policy=reward_policy)
    # fishing
    rows = list(fish.values())
    unlocked = [f for f in rows if levels["fishing"] >= int(f["fishing_level"])]
    top = sorted(unlocked, key=lambda f: int(f["tier"]))[-1] if unlocked else rows[0]
    return _apply_direct("fishing", top["display"], levels, inv, rules, rules_version,
                         game_uuid, rk, gems, ores, trees, fish, {}, {}, diagnostics,
                         reward_policy=reward_policy)


def _achievements(levels, counters) -> List[str]:
    out = []
    if counters.get("first_catch"):
        out.append("first_catch")
    if counters.get("first_cook"):
        out.append("first_cook")
    for skill in SKILLS:
        lvl = levels.get(skill, 1)
        for gate in (10, 30, 60, 99):
            if lvl >= gate:
                out.append(f"skill_{gate}_{skill}")
    if counters.get("successful_cooks", 0) >= 100:
        out.append("cooks_100")
    if counters.get("successful_cooks", 0) >= 1000:
        out.append("cooks_1000")
    return out
