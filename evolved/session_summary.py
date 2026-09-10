# evolved/session_summary.py - Per-deck study session recap (pure, Qt-free).
"""Session boundaries (decision 23):

  - A session belongs to one study deck; leaving and returning to that deck
    continues it.
  - Switching decks, restarting Anki, or changing profile/mode starts a new
    session; earned progress stays saved.
  - Leaving early triggers no popup; the live recap stays readable anywhere
    the shell renders it (Training Home banner).

Recap numbers are recalculated from the current projection's per-review
outcomes, never from a whole-account balance difference, so Undo/replay
recalculates them correctly and remote rewards never appear as session
earnings.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class ReviewSession:
    session_id: str
    deck_id: int
    started_at: float
    baseline_levels: Dict[str, int] = field(default_factory=dict)
    baseline_achievements: Set[str] = field(default_factory=set)
    review_keys: Set[str] = field(default_factory=set)
    order: List[str] = field(default_factory=list)
    completed: bool = False
    completion_shown: bool = False
    last_outcome: Optional[Dict[str, Any]] = None
    remote_notice: str = ""


def start(deck_id: int, projection: Optional[Dict[str, Any]] = None) -> ReviewSession:
    projection = projection or {}
    return ReviewSession(
        session_id=str(uuid.uuid4()),
        deck_id=int(deck_id or 0),
        started_at=time.time(),
        baseline_levels={k: int(v) for k, v in
                         (projection.get("levels") or {}).items()},
        baseline_achievements=set(projection.get("achievements") or []),
    )


def same_deck(session: Optional[ReviewSession], deck_id: int) -> bool:
    return bool(session is not None
                and int(session.deck_id) == int(deck_id or 0))


def record(session: Optional[ReviewSession], review_key: str, outcome: str,
           awarded: bool, paused: bool = False) -> None:
    if session is None or not review_key:
        return
    if review_key not in session.review_keys:
        session.review_keys.add(review_key)
        session.order.append(review_key)
    session.last_outcome = {"review_key": review_key, "outcome": str(outcome),
                            "awarded": bool(awarded), "paused": bool(paused),
                            "at": time.time()}
    if awarded or paused:
        session.completed = False  # a new eligible review keeps it live


def note_remote(session: Optional[ReviewSession], text: str) -> None:
    if session is not None and text:
        session.remote_notice = str(text)[:200]


def current_recap(session: Optional[ReviewSession],
                  projection: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Recap computed live from the projection. None when there is nothing
    worth showing (no session or no credited reviews yet)."""
    if session is None:
        return None
    projection = projection or {}
    outcomes = (projection.get("review_outcomes") or {})
    xp_by_skill: Dict[str, int] = {}
    items: Dict[str, int] = {}
    consumed: Dict[str, int] = {}
    paused = 0
    counted = 0
    for key in session.order:
        outcome = outcomes.get(key)
        if not outcome:
            continue
        counted += 1
        if outcome.get("rewarded") and int(outcome.get("xp_micro", 0)) > 0:
            skill = str(outcome.get("skill", ""))
            xp_by_skill[skill] = xp_by_skill.get(skill, 0) + int(outcome["xp_micro"])
        if outcome.get("outcome") in ("paused_materials", "paused_level"):
            paused += 1
        for name, qty in (outcome.get("items") or {}).items():
            items[str(name)] = items.get(str(name), 0) + int(qty)
        for name, qty in (outcome.get("consumed") or {}).items():
            consumed[str(name)] = consumed.get(str(name), 0) + int(qty)

    levels = projection.get("levels") or {}
    level_ups: Dict[str, int] = {}
    for skill, level in levels.items():
        before = int(session.baseline_levels.get(skill, 0) or 0)
        now = int(level or 0)
        if now > before:
            level_ups[str(skill)] = now - before
    unlocks = sorted(set(projection.get("achievements") or [])
                     - set(session.baseline_achievements))

    if counted == 0 and not session.completed and not session.order:
        return None
    return {
        "session_id": session.session_id,
        "deck_id": session.deck_id,
        "reviews": counted,
        "xp_by_skill": xp_by_skill,
        "xp_total_micro": sum(xp_by_skill.values()),
        "items": items,
        "consumed": consumed,
        "level_ups": level_ups,
        "unlocks": unlocks,
        "paused": paused,
        "completed": bool(session.completed),
        "remote_notice": session.remote_notice,
        "text": _compose_text(counted, xp_by_skill, items, consumed,
                              level_ups, unlocks, paused, session),
    }


def _compose_text(counted: int, xp_by_skill: Dict[str, int],
                  items: Dict[str, int], consumed: Dict[str, int],
                  level_ups: Dict[str, int], unlocks: List[str],
                  paused: int, session: ReviewSession) -> str:
    from .ui.menu_model import SKILL_LABELS, achievement_live_rows, format_xp
    if counted == 0:
        return ("No reviews completed in this session yet — pick a deck from "
                "the deck list and press Study Now.")
    parts = []
    xp_bits = ", ".join(f"{format_xp(v)} {SKILL_LABELS.get(k, k.title())} XP"
                        for k, v in sorted(xp_by_skill.items()) if v > 0)
    parts.append(f"This session: {xp_bits}." if xp_bits
                 else "This session earned no XP.")
    if items:
        item_bits = ", ".join(f"{name} ×{qty}" for name, qty in
                              sorted(items.items()))
        parts.append(f"Gained: {item_bits}.")
    if consumed:
        used_bits = ", ".join(f"{name} ×{qty}" for name, qty in
                              sorted(consumed.items()))
        parts.append(f"Used: {used_bits}.")
    if level_ups:
        level_bits = ", ".join(f"{SKILL_LABELS.get(k, k.title())} "
                               f"+{v} level{'' if v == 1 else 's'}"
                               for k, v in sorted(level_ups.items()))
        parts.append(f"Level up: {level_bits}.")
    if unlocks:
        names = []
        for aid in unlocks:
            rows = achievement_live_rows({"achievements": {"required": [aid]}}, [aid])
            names.append(rows[0]["name"] if rows else aid)
        parts.append("Unlocked: " + ", ".join(names) + ".")
    if paused:
        parts.append(f"{paused} review{'' if paused == 1 else 's'} paused for "
                     "missing materials or level.")
    if session.remote_notice:
        parts.append(session.remote_notice)
    return " ".join(parts)


def close(session: Optional[ReviewSession]) -> None:
    if session is not None:
        session.completed = False
        session.review_keys.clear()
        session.order.clear()
