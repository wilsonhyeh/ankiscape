# evolved/ui/menu_model.py - Pure menu content builders (Qt-free, tested).
"""Row builders for the Evolved tabbed menu. Qt shells in menu.py render these.
Skills show level/XP/resource and materials; Hiscores shows last success,
pending progress, offline/auth/problem status; Settings validates presets.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import SKILLS

CATCHUP_PRESETS = ("mining", "woodcutting", "fishing")
GATHERING_SKILLS = ("mining", "woodcutting", "fishing")
SKILL_LABELS = {
    "mining": "Mining", "woodcutting": "Woodcutting", "smithing": "Smithing",
    "crafting": "Crafting", "fishing": "Fishing", "cooking": "Cooking",
}
MICRO = 1_000_000


def format_xp(micro) -> str:
    """Exact XP display: divide micro-XP by 1,000,000 without floats."""
    try:
        value = int(micro)
    except (TypeError, ValueError):
        return "0"
    sign = "-" if value < 0 else ""
    whole, frac = divmod(abs(value), MICRO)
    if frac == 0:
        return f"{sign}{whole:,}"
    digits = f"{frac:06d}".rstrip("0")
    return f"{sign}{whole:,}.{digits}"


def _table_for(rules: Dict[str, Any], skill: str):
    return {
        "mining": (rules.get("ores", []), "level", "base_xp"),
        "woodcutting": (rules.get("trees", []), "level", "base_xp"),
        "fishing": (rules.get("fish", []), "fishing_level", "fishing_base_xp"),
        "smithing": (rules.get("bars", []), "level", "base_xp"),
        "crafting": (rules.get("crafting", []), "level", "base_xp"),
        "cooking": (rules.get("fish", []), "cooking_level", "cooking_base_xp"),
    }.get(skill, ([], "level", "base_xp"))


def requirements_for(rules: Dict[str, Any], skill: str,
                     selection: str) -> Dict[str, int]:
    table, level_key, _xp_key = _table_for(rules, skill)
    _ = level_key
    for entry in table:
        if entry.get("display") == selection:
            if skill == "cooking":
                return {selection: 1}
            return dict(entry.get("ore_required",
                                  entry.get("requirements", {})))
    return {}


def skill_level_requirement(rules: Dict[str, Any], skill: str,
                            selection: str) -> int:
    table, level_key, _xp_key = _table_for(rules, skill)
    for entry in table:
        if entry.get("display") == selection:
            return int(entry.get(level_key, 1))
    return 1


def missing_materials_text(needs: Dict[str, int],
                           have: Dict[str, int]) -> str:
    parts = []
    for name, required in needs.items():
        missing = int(required) - int(have.get(name, 0))
        if missing > 0:
            parts.append(f"{missing}× {name}")
    return ", ".join(parts)


def resource_entries(rules: Dict[str, Any], skill: str, levels: Dict[str, int],
                     inventory: Dict[str, int],
                     selection: str) -> List[Dict[str, Any]]:
    """All tiers for one skill: preview data + lock/selected/ready state.

    Preview NEVER changes training; callers commit only from Train.
    """
    table, level_key, xp_key = _table_for(rules, skill)
    level = int(levels.get(skill, 1))
    out = []
    for entry in table:
        display = str(entry.get("display", ""))
        req_level = int(entry.get(level_key, 1))
        needs = requirements_for(rules, skill, display)
        have = {name: int(inventory.get(name, 0)) for name in needs}
        mats_ok = all(have[name] >= qty for name, qty in needs.items())
        locked = level < req_level
        out.append({
            "display": display,
            "tier": int(entry.get("tier", 0)),
            "level": req_level,
            "base_xp": entry.get(xp_key, entry.get("base_xp", 0)),
            "probability": entry.get("probability"),
            "selected": display == selection,
            "locked": locked,
            "needs": needs,
            "have": have,
            "materials_ready": mats_ok,
            "missing_text": ("" if mats_ok
                             else missing_materials_text(needs, have)),
            "gather": skill in GATHERING_SKILLS,
        })
    return sorted(out, key=lambda r: (r["tier"], r["display"]))


def skill_entries(rules: Dict[str, Any], levels: Dict[str, int],
                  xp_micro: Dict[str, int], selections: Dict[str, str],
                  active_skill: str = "") -> List[Dict[str, Any]]:
    """Six-skill grid rows with level and progress for tile rendering."""
    out = []
    thresholds = rules.get("thresholds", [])
    for skill in SKILLS:
        level = int(levels.get(skill, 1))
        if level >= 99:
            to_next = "maxed"
        else:
            try:
                nxt = int(thresholds[level]) * MICRO
                have = max(0, int(xp_micro.get(skill, 0)))
                to_next = format_xp(max(0, nxt - have))
            except (IndexError, TypeError, ValueError):
                to_next = "?"
        out.append({"skill": skill, "label": SKILL_LABELS[skill],
                    "level": level, "xp_micro": int(xp_micro.get(skill, 0)),
                    "xp_display": format_xp(int(xp_micro.get(skill, 0))),
                    "to_next": to_next, "maxed": level >= 99,
                    "selection": selections.get(skill, ""),
                    "trained": skill == active_skill})
    return out


def _produce_consume_maps(rules: Dict[str, Any]) -> Dict[str, Dict[str, List[str]]]:
    """Ingredient/output relationships from rules, never display-name guesses."""
    relations: Dict[str, Dict[str, List[str]]] = {}
    tables = {
        "mining": (rules.get("ores", []), "display", {}),
        "woodcutting": (rules.get("trees", []), "display", {}),
        "fishing": (rules.get("fish", []), "display", {}),
        "smithing": (rules.get("bars", []), "display", "ore_required"),
        "crafting": (rules.get("crafting", []), "display", "requirements"),
    }
    for skill, (entries, display_key, req_key) in tables.items():
        for entry in entries:
            display = str(entry.get(display_key, ""))
            if not display:
                continue
            rel = relations.setdefault(display, {"produced_by": [],
                                                 "consumed_by": []})
            if skill not in rel["produced_by"]:
                rel["produced_by"].append(skill)
            reqs = entry.get(req_key, {}) if req_key else {}
            for ingredient in reqs:
                sub = relations.setdefault(str(ingredient),
                                           {"produced_by": [], "consumed_by": []})
                if skill not in sub["consumed_by"]:
                    sub["consumed_by"].append(skill)
    for entry in rules.get("fish", []):
        display = str(entry.get("display", ""))
        if display:
            rel = relations.setdefault(f"Cooked {display}",
                                       {"produced_by": [], "consumed_by": []})
            if "cooking" not in rel["produced_by"]:
                rel["produced_by"].append("cooking")
    return relations


def bank_entries(rules: Dict[str, Any], inventory: Dict[str, int],
                 *, query: str = "", skill: str = "") -> List[Dict[str, Any]]:
    """Bank grid rows: qty + producing/consuming skills, never display guesses."""
    relations = _produce_consume_maps(rules)
    q = (query or "").strip().casefold()
    rows = []
    for name, qty in inventory.items():
        try:
            amount = int(qty)
        except (TypeError, ValueError):
            continue
        if amount <= 0:
            continue
        rel = relations.get(name, {"produced_by": [], "consumed_by": []})
        if q and q not in str(name).casefold():
            continue
        if skill and skill not in rel["produced_by"] and skill not in rel["consumed_by"]:
            continue
        rows.append({"display": str(name), "qty": amount,
                     "produced_by": list(rel["produced_by"]),
                     "consumed_by": list(rel["consumed_by"]),
                     "known": name in relations})
    return sorted(rows, key=lambda r: r["display"].casefold())


def bank_skills_for(rules: Dict[str, Any], display: str) -> Dict[str, List[str]]:
    relations = _produce_consume_maps(rules)
    return relations.get(str(display),
                         {"produced_by": [], "consumed_by": []})


_ACHIEVEMENT_NAMES = {
    "first_catch": ("First Catch", "Catch your first fish"),
    "first_cook": ("First Cook", "Cook your first fish"),
    "cooks_100": ("Campfire Cook", "Cook 100 fish"),
    "cooks_1000": ("Grand Chef", "Cook 1,000 fish"),
}


def achievement_live_rows(rules: Dict[str, Any], unlocked: List[str],
                          levels: Optional[Dict[str, int]] = None,
                          counters: Optional[Dict[str, int]] = None
                          ) -> List[Dict[str, Any]]:
    """Human-readable achievement rows with requirement + progress.

    Never exposes raw internal ids as display text.
    """
    levels = levels or {}
    counters = counters or {}
    have = set(unlocked or [])
    out = []
    for aid in rules.get("achievements", {}).get("required", []):
        name, requirement, progress = aid, aid, ""
        if aid in _ACHIEVEMENT_NAMES:
            name, requirement = _ACHIEVEMENT_NAMES[aid]
            if aid == "first_catch":
                progress = "done" if counters.get("first_catch") else "not yet"
            elif aid == "first_cook":
                progress = "done" if counters.get("first_cook") else "not yet"
            elif aid == "cooks_100":
                progress = f"{int(counters.get('successful_cooks', 0))}/100"
            elif aid == "cooks_1000":
                progress = f"{int(counters.get('successful_cooks', 0))}/1,000"
        elif aid.startswith("skill_"):
            parts = aid.split("_")
            if len(parts) == 3:
                try:
                    gate = int(parts[1])
                except ValueError:
                    gate = 0
                skill = parts[2]
                label = SKILL_LABELS.get(skill, skill.title())
                name = f"{label} {gate}"
                requirement = f"Reach level {gate} {label}"
                current = int(levels.get(skill, 1))
                progress = f"level {current}/{gate}"
        out.append({"id": aid, "name": name, "requirement": requirement,
                    "progress": progress, "unlocked": aid in have})
    return out


def skill_rows(rules: Dict[str, Any], levels: Dict[str, int],
               xp_micro: Dict[str, int], inventory: Dict[str, int],
               selections: Dict[str, str]) -> List[Dict[str, Any]]:
    """One row per skill: level, XP, selected resource, material availability."""
    rows = []
    for skill in SKILLS:
        rows.append({"skill": skill, "level": int(levels.get(skill, 1)),
                     "xp_micro": int(xp_micro.get(skill, 0)),
                     "xp_display": str(int(xp_micro.get(skill, 0))),
                     "selection": selections.get(skill, ""),
                     "materials": _materials_for(skill, rules, inventory,
                                                 selections.get(skill, ""))})
    return rows


def _materials_for(skill: str, rules: Dict[str, Any], inventory: Dict[str, int],
                   selection: str) -> Dict[str, Any]:
    tables = {"smithing": ({b["display"]: b for b in rules.get("bars", [])}, "ore_required"),
              "crafting": ({c["display"]: c for c in rules.get("crafting", [])}, "requirements"),
              "cooking": ({f["display"]: {**f, "requirements": {f["display"]: 1}}
                           for f in rules.get("fish", [])}, "requirements")}
    if skill in ("mining", "woodcutting", "fishing"):
        return {"needs": {}, "ready": True, "note": "gather"}
    table, req_key = tables.get(skill, ({}, "requirements"))
    spec = table.get(selection)
    if not spec:
        return {"needs": {}, "ready": False, "note": "no selection"}
    reqs = spec.get(req_key, {})
    have = {name: int(inventory.get(name, 0)) for name in reqs}
    ready = all(have[n] >= q for n, q in reqs.items())
    return {"needs": reqs, "have": have, "ready": ready,
            "note": "" if ready else "gather materials first"}


def achievement_rows(required: List[str], unlocked: List[str]) -> List[Dict[str, Any]]:
    have = set(unlocked)
    return [{"id": aid, "unlocked": aid in have} for aid in required]


def hiscores_status(*, logged_in: bool, last_success: Any, pending: int,
                    last_error: str = "") -> str:
    if not logged_in:
        return "offline — log in to sync and view hiscores"
    if last_error:
        return f"problem: {last_error}"
    if last_success:
        return f"last sync {last_success}; pending {pending}"
    if pending:
        return f"never synced; pending {pending}"
    return "online — never synced"


def validate_preset(skill: str) -> str:
    norm = (skill or "").strip().lower()
    if norm not in CATCHUP_PRESETS:
        raise ValueError(f"preset must be one of {CATCHUP_PRESETS}")
    return norm


def training_home(rules: Dict[str, Any], projection: Dict[str, Any],
                  selections: Dict[str, str], active_skill: str) -> Dict[str, Any]:
    """Training Home content: current skill/resource, expectations, pause state.

    Pause rules (policy 2): unmet level OR missing materials both mean the next
    eligible production review earns zero until resolved. Gathering pauses only
    on an unmet level (Undo can lock a resource below its requirement).
    """
    from ..logic_pure import MICRO, multiplied_base_micro, quarter_half_up

    skill = str(active_skill or "mining").lower()
    if skill not in SKILLS:
        skill = "mining"
    levels = projection.get("levels", {}) or {}
    xp_micro = projection.get("xp_micro", {}) or {}
    inventory = projection.get("inventory", {}) or {}
    thresholds = rules.get("thresholds", [])
    level = int(levels.get(skill, 1))
    selection = str(selections.get(skill, "") or "")
    table, level_key, xp_key = _table_for(rules, skill)
    spec = None
    for entry in table:
        if entry.get("display") == selection:
            spec = entry
            break
    if spec is None and table:
        spec = sorted(table, key=lambda e: int(e.get("tier", 0)))[0]
        selection = str(spec.get("display", ""))
    req_level = int(spec.get(level_key, 1)) if spec else 1
    level_ok = level >= req_level
    needs = requirements_for(rules, skill, selection)
    have = {name: int(inventory.get(name, 0)) for name in needs}
    mats_ok = all(have[name] >= qty for name, qty in needs.items())
    gather = skill in GATHERING_SKILLS

    base_xp = spec.get(xp_key, spec.get("base_xp", 0)) if spec else 0
    tier = int(spec.get("tier", 1)) if spec else 1
    success_xp = multiplied_base_micro(base_xp, tier) if spec else 0
    fail_xp = max(quarter_half_up(success_xp), MICRO) if spec else 0
    if gather:
        burn_xp = 0
    else:
        burn_xp = fail_xp  # burn / failed production share the 25% rule

    pause_reason = ""
    if not level_ok:
        pause_reason = "level"
    elif not gather and not mats_ok:
        pause_reason = "materials"

    xp_have = max(0, int(xp_micro.get(skill, 0)))
    if level >= 99:
        to_next = "maxed"
        progress = 1.0
    else:
        try:
            nxt = int(thresholds[level]) * MICRO
            to_next = format_xp(max(0, nxt - xp_have))
        except (IndexError, TypeError, ValueError):
            to_next = "?"
        progress = _progress(xp_have, thresholds, level)

    unlocked = [e for e in table
                if level >= int(e.get(level_key, 1))]
    next_unlock = None
    for entry in sorted(table, key=lambda e: int(e.get("tier", 0))):
        if level < int(entry.get(level_key, 1)):
            next_unlock = {"display": str(entry.get("display", "")),
                           "level": int(entry.get(level_key, 1))}
            break
    maxed = all(level >= int(e.get(level_key, 1)) for e in table) if table else False
    _ = unlocked

    return {
        "skill": skill, "skill_label": SKILL_LABELS.get(skill, skill.title()),
        "resource": selection,
        "level": level, "xp_micro": xp_have, "xp_display": format_xp(xp_have),
        "xp_to_next": to_next, "progress": progress, "max_level": level >= 99,
        "gather": gather,
        "level_requirement": req_level,
        "needs": needs, "have": have, "materials_ready": mats_ok,
        "missing_text": ("" if mats_ok
                         else missing_materials_text(needs, have)),
        "paused": bool(pause_reason),
        "pause_reason": pause_reason,
        "expected_success_xp": format_xp(success_xp),
        "expected_fail_xp": ("" if gather else format_xp(burn_xp)),
        "expected_fail_label": ("Burn" if skill == "cooking" else "Failed"),
        "next_unlock": next_unlock,
        "all_unlocked": bool(maxed),
        "base_xp": base_xp,
    }


def _progress(xp_micro: int, thresholds, level: int) -> float:
    try:
        lvl = int(level)
        if lvl >= 99:
            return 1.0
        if lvl < 1:
            lvl = 1
        base = int(thresholds[lvl - 1]) * MICRO
        nxt = int(thresholds[lvl]) * MICRO
        span = nxt - base
        if span <= 0:
            return 0.0
        return max(0.0, min(1.0, (int(xp_micro) - base) / float(span)))
    except (IndexError, TypeError, ValueError):
        return 0.0


def format_hiscores_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Competition ranking display: ties share a rank (server computes rank).

    XP arrives in micro units; display divides exactly, never raw micro.
    """
    out = []
    for row in rows:
        out.append({"rank": int(row.get("rank", 0)),
                    "username": str(row.get("username", "?")),
                    "xp": str(row.get("xp", 0)),
                    "xp_display": format_xp(row.get("xp", 0))})
    return out
