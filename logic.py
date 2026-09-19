from .constants import (
    ORE_DATA,
    EXP_TABLE,
    ACHIEVEMENTS,
    BASE_WOODCUTTING_PROBABILITY,
    BASE_MINING_PROBABILITY,
    LEVEL_BONUS_FACTOR,
)
from .ui import show_level_up_dialog, show_achievement_dialog
from .debug import debug_log

"""Anki-aware game logic orchestrators (no direct persistence here)."""

def get_exp_to_next_level(player_data, EXP_TABLE):
    current_level = player_data["mining_level"]
    if current_level >= 99:
        return 0
    return EXP_TABLE[current_level] - player_data["total_exp"]

from .logic_pure import (
    calculate_new_level,
    calculate_probability_with_level,
    get_newly_completed_achievements,
)


def _popups_enabled() -> bool:
    """Whether level-up/achievement popups are enabled (user setting, default on)."""
    try:
        from aqt import mw  # type: ignore
        if getattr(mw, "col", None):
            return bool(mw.col.get_config("ankiscape_popups_enabled", True))
    except Exception:
        pass
    return True


def level_up_check(skill, player_data):
    """Advance the stored level to match stored exp.

    State mutation only — no dialogs. Mutating and presenting are deliberately
    separate so the caller can persist BEFORE anything modal runs: a UI failure
    must never discard a level the player earned (3.0.0 durability fix, D-3).

    Returns the levels gained, for `show_level_up_popups` to present.
    """
    skill_map = {
        "Mining": ("mining_level", "mining_exp"),
        "Woodcutting": ("woodcutting_level", "woodcutting_exp"),
        "Smithing": ("smithing_level", "smithing_exp"),
        "Crafting": ("crafting_level", "crafting_exp"),
    }
    if skill not in skill_map:
        return []
    level_key, exp_key = skill_map[skill]
    old_level = player_data[level_key]
    new_level = calculate_new_level(player_data[exp_key], old_level, EXP_TABLE)
    if new_level <= old_level:
        return []
    player_data[level_key] = new_level
    return list(range(old_level + 1, new_level + 1))


def show_level_up_popups(skill, levels):
    """Present one level-up dialog per level gained. Best-effort; never raises.

    Runs after the reward is durably saved, so a UI failure here is cosmetic
    and must not propagate into Anki's answer path.
    """
    if not levels:
        return
    try:
        if not _popups_enabled():
            return
        for _level in levels:
            show_level_up_dialog(skill)
    except Exception as exc:
        debug_log(f"level-up popup failed for {skill}: {exc!r}")


def check_achievements(player_data):
    """Record newly completed achievements.

    State mutation only — no dialogs, for the same durability reason as
    `level_up_check`. Returns the achievements newly recorded.
    """
    newly_completed = get_newly_completed_achievements(player_data, ACHIEVEMENTS)
    for achievement in newly_completed:
        player_data["completed_achievements"].append(achievement)
    return newly_completed


def show_achievement_popups(achievements):
    """Present one dialog per newly completed achievement. Best-effort; never raises."""
    if not achievements:
        return
    try:
        if not _popups_enabled():
            return
        for achievement in achievements:
            show_achievement_dialog(achievement, ACHIEVEMENTS[achievement])
    except Exception as exc:
        debug_log(f"achievement popup failed: {exc!r}")


def calculate_woodcutting_probability(player_level: int, tree_probability: float) -> float:
    return calculate_probability_with_level(
        player_level,
        BASE_WOODCUTTING_PROBABILITY,
        LEVEL_BONUS_FACTOR,
        tree_probability,
        cap=0.95,
    )


def calculate_mining_probability(player_level: int, ore_probability: float) -> float:
    return calculate_probability_with_level(
        player_level,
        BASE_MINING_PROBABILITY,
        LEVEL_BONUS_FACTOR,
        ore_probability,
        cap=0.95,
    )
