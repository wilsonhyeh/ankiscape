# evolved/onboarding.py - Resumable first-run/upgrade model (pure, Qt-free).
"""Decision + persistence for the guided Evolved setup:

  Welcome -> gathering skill -> level-1 resource -> how rewards work -> done.

The draft is stored separately from active training so closing setup resumes
exactly where it left off and awards nothing in the meantime. Activation time
is stamped only when setup commits, so reviews taken before completion can
never be backfilled by catch-up.

Freshness detection uses meaningful Classic progress and an existing Evolved
game identity — never the mere presence of a default mode key.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

DRAFT_KEY = "ankiscape_evolved_onboarding"
EVOLVED_POINTER_KEY = "ankiscape_evolved_player_data"

STEPS = ("welcome", "skill", "resource", "explain", "done")
GATHERING_SKILLS = ("mining", "woodcutting", "fishing")

# First-run action values returned by decide_first_load.
ACTIVATE_CLASSIC = "activate_classic"
ACTIVATE_EVOLVED = "activate_evolved"
SHOW_UPGRADE = "show_upgrade"
RESUME_ONBOARDING = "resume_onboarding"


@dataclass
class OnboardingState:
    step: str = "welcome"
    gathering_skill: str = ""
    starting_resource: str = ""
    complete: bool = False
    updated_at: int = 0
    repairs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"step": self.step,
                "gathering_skill": self.gathering_skill,
                "starting_resource": self.starting_resource,
                "complete": bool(self.complete),
                "updated_at": int(self.updated_at or time.time())}


def _valid_step(step: str) -> bool:
    return step in STEPS


def from_dict(data: Optional[Dict[str, Any]]) -> OnboardingState:
    """Tolerant decode. Missing/corrupt fields are repaired WITHOUT touching
    earned progress (the draft is disposable); repairs are reported."""
    repairs: List[str] = []
    if not isinstance(data, dict):
        if data is not None:
            repairs.append("draft was not an object; restarting setup")
        return OnboardingState(repairs=repairs)
    step = data.get("step", "welcome")
    if not _valid_step(step):
        repairs.append(f"unknown step {step!r}; returning to Welcome")
        step = "welcome"
    skill = str(data.get("gathering_skill", "") or "").lower()
    if skill and skill not in GATHERING_SKILLS:
        repairs.append(f"unknown gathering skill {skill!r}; cleared")
        skill = ""
    resource = str(data.get("starting_resource", "") or "")
    complete = bool(data.get("complete", False))
    if complete and (not skill or not resource):
        repairs.append("completion marker missing selections; setup re-opened")
        complete = False
        step = "skill" if not skill else "resource"
    try:
        updated = int(data.get("updated_at", 0) or 0)
    except (TypeError, ValueError):
        updated = 0
        repairs.append("bad timestamp; reset")
    return OnboardingState(step=step, gathering_skill=skill,
                           starting_resource=resource, complete=complete,
                           updated_at=updated, repairs=repairs)


def load(get_config) -> OnboardingState:
    try:
        raw = get_config(DRAFT_KEY, None)
    except Exception:
        raw = None
    return from_dict(raw)


def save(set_config, state: OnboardingState) -> Dict[str, Any]:
    state.updated_at = int(time.time())
    payload = state.to_dict()
    try:
        set_config(DRAFT_KEY, payload)
        return {"ok": True, "state": payload}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)[:200], "state": payload}


def level_one_resource(rules: Dict[str, Any], skill: str) -> str:
    """The tier-1 display for a gathering skill (used in the setup preview)."""
    skill = str(skill or "").lower()
    table = {"mining": rules.get("ores", []),
             "woodcutting": rules.get("trees", []),
             "fishing": rules.get("fish", [])}.get(skill, [])
    if not table:
        return ""
    first = sorted(table, key=lambda e: int(e.get("tier", 0)))[0]
    return str(first.get("display", ""))


def validate_resource(rules: Dict[str, Any], skill: str, resource: str) -> bool:
    skill = str(skill or "").lower()
    resource = str(resource or "")
    if skill not in GATHERING_SKILLS or not resource:
        return False
    table = {"mining": rules.get("ores", []),
             "woodcutting": rules.get("trees", []),
             "fishing": rules.get("fish", [])}.get(skill, [])
    return any(str(e.get("display", "")) == resource for e in table)


def _is_int(value) -> bool:
    try:
        int(value)
        return True
    except (TypeError, ValueError):
        return False


def has_meaningful_classic_progress(player_data: Any) -> bool:
    """True when Classic has real saved progress (not just a default shape)."""
    if not isinstance(player_data, dict):
        return False
    for key, value in player_data.items():
        if str(key).endswith("_exp"):
            try:
                if int(value) > 0:
                    return True
            except (TypeError, ValueError):
                continue
        if key in ("inventory", "achievements") and value:
            if isinstance(value, dict) and any(int(v) > 0 for v in value.values()
                                               if _is_int(v)):
                return True
            if isinstance(value, list) and value:
                return True
    if player_data.get("current_skill") not in (None, "", "None"):
        return True
    return False


def evolved_identity(get_config) -> Optional[Dict[str, Any]]:
    try:
        pointer = get_config(EVOLVED_POINTER_KEY, None)
    except Exception:
        pointer = None
    if isinstance(pointer, dict) and pointer.get("game_uuid"):
        return pointer
    return None


def identity_active(pointer: Optional[Dict[str, Any]]) -> bool:
    """A game identity counts as active only once activation was stamped."""
    if not isinstance(pointer, dict):
        return False
    try:
        return int(pointer.get("activated_at", 0) or 0) > 0
    except (TypeError, ValueError):
        return False


# One-time acknowledgement shown before the first Classic -> Evolved switch.
# Shared verbatim by the upgrade dialog and the standalone notice.
FRESH_START_TITLE = "Evolved starts fresh."
FRESH_START_BODY = (
    "Every AnkiScape Evolved player starts at level 1. Your Classic levels, "
    "XP and items are NOT transferred \u2014 they stay with Classic exactly "
    "as they are. You can stay on Classic, or switch between Classic and "
    "Evolved whenever you like; each mode keeps its own progress and nothing "
    "restarts. There is no Classic-to-Evolved transfer; Evolved has its own "
    "shared leaderboard.")
FRESH_START_ACK_TEXT = (
    "I understand that Evolved starts fresh at level 1 and my Classic "
    "progress cannot be transferred.")


def fresh_start_gate_required(evolved: Optional[Dict[str, Any]]) -> bool:
    """True when the first-switch acknowledgement is required.

    No identity at all, or a UUID without a valid positive activation stamp,
    both count as unfinished setup.
    """
    return not identity_active(evolved)


def decide_first_load(*, requested: Optional[str], has_request: bool,
                      classic_progress: bool,
                      evolved: Optional[Dict[str, Any]],
                      draft: OnboardingState) -> Dict[str, Any]:
    """Pure first-load routing.

    Returns {action, mode, reason}. `requested` may be None when unset.
    """
    mode = str(requested or "").lower()
    draft_started = bool(draft.gathering_skill or draft.step != "welcome"
                         or draft.updated_at)
    if mode == "evolved":
        if draft.complete or identity_active(evolved):
            return {"action": ACTIVATE_EVOLVED, "mode": "evolved",
                    "reason": "explicit_evolved"}
        return {"action": RESUME_ONBOARDING, "mode": "evolved",
                "reason": "explicit_evolved_incomplete"}
    if mode == "classic":
        return {"action": ACTIVATE_CLASSIC, "mode": "classic",
                "reason": "explicit_classic"}
    # No explicit request.
    if identity_active(evolved):
        # Established Evolved games never re-enter setup, even if the
        # disposable draft was lost or corrupted.
        return {"action": ACTIVATE_EVOLVED, "mode": "evolved",
                "reason": "established_evolved"}
    if draft_started and not draft.complete:
        return {"action": RESUME_ONBOARDING, "mode": "evolved",
                "reason": "draft_resume"}
    if classic_progress:
        return {"action": SHOW_UPGRADE, "mode": "classic",
                "reason": "classic_upgrade"}
    return {"action": RESUME_ONBOARDING, "mode": "evolved",
            "reason": "fresh_install"}


def advance(state: OnboardingState, rules: Dict[str, Any]) -> OnboardingState:
    """Move one step forward, validating selections as it goes."""
    if state.step == "welcome":
        state.step = "skill"
    elif state.step == "skill":
        if state.gathering_skill in GATHERING_SKILLS:
            state.step = "resource"
            if not state.starting_resource:
                state.starting_resource = level_one_resource(
                    rules, state.gathering_skill)
    elif state.step == "resource":
        if validate_resource(rules, state.gathering_skill,
                             state.starting_resource):
            state.step = "explain"
    elif state.step == "explain":
        if validate_resource(rules, state.gathering_skill,
                             state.starting_resource):
            state.step = "done"
            state.complete = True
    return state


def back(state: OnboardingState) -> OnboardingState:
    order = {step: idx for idx, step in enumerate(STEPS)}
    idx = order.get(state.step, 0)
    state.step = STEPS[max(0, idx - 1)]
    return state


def step_number(state: OnboardingState) -> Tuple[int, int]:
    order = {step: idx + 1 for idx, step in enumerate(STEPS)}
    return (order.get(state.step, 1), len(STEPS))
