# evolved/ui/menu_model.py - Pure menu content builders (Qt-free, tested).
"""Row builders for the Evolved tabbed menu. Qt shells in menu.py render these.
Skills show level/XP/resource and materials; Hiscores shows last success,
pending progress, offline/auth/problem status; Settings validates presets.
"""
from __future__ import annotations

from typing import Any, Dict, List

from . import SKILLS

CATCHUP_PRESETS = ("mining", "woodcutting", "fishing")


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


def format_hiscores_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Competition ranking display: ties share a rank (server computes rank)."""
    out = []
    for row in rows:
        out.append({"rank": int(row.get("rank", 0)),
                    "username": str(row.get("username", "?")),
                    "xp": str(row.get("xp", 0))})
    return out
