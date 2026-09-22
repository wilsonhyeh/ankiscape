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


# Register-notice variants (S13): "upgrade" protects an existing offline game,
# "first_run" covers a profile with nothing to preserve.
NOTICE_UPGRADE = "upgrade"
NOTICE_FIRST_RUN = "first_run"


def active_local_game(*, pointer: Optional[Dict[str, Any]],
                      journal_exists: bool, journal_has_binding: bool,
                      account_game_uuid: str = "") -> bool:
    """S13: does this profile currently have an active LOCAL (offline) game?

    Operational against pointer/binding/slots state only — no network, no
    mint, no engine build. True iff the pointer exists with a non-null
    game_uuid, that uuid is NOT the account game (it has no `account_binding`
    in its journal and is not the known account_game_uuid slot), and a journal
    exists for it at journal_path_for_profile(profile_dir, game_uuid).
    """
    if not isinstance(pointer, dict):
        return False
    game_uuid = str(pointer.get("game_uuid") or "")
    if not game_uuid:
        return False
    if str(account_game_uuid or "") == game_uuid:
        return False
    if journal_has_binding:
        return False
    return bool(journal_exists)


def register_notice_variant(*, pointer: Optional[Dict[str, Any]],
                            journal_exists: bool, journal_has_binding: bool,
                            account_game_uuid: str = "") -> str:
    """S13: the register-notice variant, decided by the predicate above."""
    if active_local_game(pointer=pointer, journal_exists=journal_exists,
                         journal_has_binding=journal_has_binding,
                         account_game_uuid=account_game_uuid):
        return NOTICE_UPGRADE
    return NOTICE_FIRST_RUN


def link_slots(*, prior_active: str, account_game_uuid: str) -> Dict[str, str]:
    """R14/S7 step 5: the pointer slots a login records.

    The local slot is written only when the prior active game was a different
    (offline) game; a re-login never moves the account game into the local
    slot.
    """
    account = str(account_game_uuid)
    slots = {"account_game_uuid": account}
    prior = str(prior_active or "")
    if prior and prior != account:
        slots["local_game_uuid"] = prior
    return slots


def coordinator_offer_uuid(*, engine_uuid: str, pointer_uuid: str,
                           engine_available: bool) -> str:
    """S12: the uuid the coordinator offers as `p_game_uuid`.

    With a live local engine, its uuid is offered. With no local game at all,
    ANY uuid is offered (a fresh uuid4 purely as the argument; D5 makes the
    server's return authoritative). An empty string means the genuine-failure
    early return: a game exists but its engine did not build.
    """
    engine_uuid = str(engine_uuid or "")
    if engine_uuid:
        return engine_uuid
    pointer_uuid = str(pointer_uuid or "")
    if pointer_uuid and not engine_available:
        return ""
    import uuid as _uuid

    return str(_uuid.uuid4())


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


def resume_after_signin(state: OnboardingState, *, signed_in: bool) -> bool:
    """D6 continuation (Wilson defect 2026-09-21): after an account-window
    sign-in, a draft still parked on Welcome must advance into setup —
    otherwise the user lands back on "Create account" having just created
    one. True only for a signed-in, incomplete draft AT welcome: mid-flow
    and completed setup are never touched by this path."""
    return bool(signed_in) and (not state.complete) \
        and state.step == "welcome"


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
