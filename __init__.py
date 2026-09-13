# __init__.py

from .constants import (
    ORE_DATA,
    TREE_DATA,
    BAR_DATA,
    GEM_DATA,
    CRAFTING_DATA,
    ORE_IMAGES,
    TREE_IMAGES,
    BAR_IMAGES,
    GEM_IMAGES,
    CRAFTED_ITEM_IMAGES,
)
from aqt import mw, gui_hooks
from anki.hooks import addHook, wrap
from aqt.reviewer import Reviewer
import time
import random
import os
import datetime
from .logic_pure import (
    calculate_probability_with_level,
    pick_gem,
    can_smelt_any_bar_pure,
    create_soft_clay_pure,
    has_crafting_materials_pure,
    can_craft_item_pure,
    apply_crafting_pure,
    apply_smelt_pure,
    apply_woodcutting_pure,
    apply_mining_pure,
    can_mine_ore_pure,
    can_cut_tree_pure,
)
from .logic import level_up_check, check_achievements, calculate_woodcutting_probability, calculate_mining_probability
from .ui import (
    ExpPopup,
    show_error_message,
    show_tree_selection_dialog,
    show_ore_selection_dialog,
    refresh_skill_availability,
    is_main_menu_open,
    focus_main_menu_if_open,
    ensure_review_hud,
    update_review_hud,
    hide_review_hud,
    migrate_legacy_settings,
)
from . import ui
from .deck_injection_pure import DeckBrowserContent as _DBC, inject_into_deck_browser_content
from .injectors import inject_reviewer_floating_button as _inject_reviewer_floating_button
from .injectors import inject_overview_floating_button as _inject_overview_floating_button
from .injectors import register_deck_browser_button as _register_deck_browser_button
from .injectors import force_deck_browser_refresh as _force_deck_browser_refresh
from .storage import load_player_data as storage_load_player_data, save_player_data as storage_save_player_data

global card_turned, exp_awarded, answer_shown

answer_shown = False
card_turned = False
exp_awarded = False

current_skill = "None"

# --- Debug logging (centralized) ---
from .debug import debug_log  # size-rotated, disabled by default unless ANKISCAPE_DEBUG=1
try:
    from .debug import set_debug_enabled as _set_debug_enabled, is_debug_enabled as _is_debug_enabled
except Exception:
    def _set_debug_enabled(_enabled: bool) -> None:
        pass
    def _is_debug_enabled() -> bool:
        return False

# Guard to avoid duplicate registrations
_ANKISCAPE_HOOKS_REGISTERED = False
_LAST_MENU_OPEN_TS = 0.0

def show_review_popup():
    ui.show_review_popup()


# Classes moved to ui.py


def save_player_data():
    storage_save_player_data(player_data, current_skill)


def load_player_data():
    global player_data, current_skill
    player_data, current_skill = storage_load_player_data()
    ui.update_menu_visibility(current_skill)

## Removed legacy get_exp_to_next_level stub; use logic_pure.get_exp_to_next_level in tests/pure logic.

# UI functions

def initialize_skill():
    global current_skill
    current_skill = mw.col.get_config("ankiscape_current_skill", default="None")
    ui.update_menu_visibility(current_skill)


def _initialize_debug_from_config():
    try:
        enabled = False
        if mw and getattr(mw, 'col', None):
            # New: developer mode controls logging
            enabled = bool(mw.col.get_config("ankiscape_developer_mode", False))
            # Back-compat: honor previous key if present and new key missing
            if not enabled:
                enabled = bool(mw.col.get_config("ankiscape_debug_enabled", False))
        _set_debug_enabled(enabled)
        if enabled:
            debug_log("debug: enabled from config on profile load (developer mode)")
    except Exception:
        pass


# --- Small helpers to reduce duplication ---
def _show_exp(exp_gained) -> None:
    """Ensure the ExpPopup exists and display exp."""
    try:
        # Respect user setting for floating XP (default True)
        show_xp = True
        try:
            if mw and getattr(mw, 'col', None):
                show_xp = bool(mw.col.get_config("ankiscape_floating_xp_enabled", True))
        except Exception:
            show_xp = True
        if show_xp:
            if not hasattr(mw, 'exp_popup'):
                mw.exp_popup = ExpPopup(mw)
            mw.exp_popup.show_exp(exp_gained)
        # Keep HUD progress in sync with new XP
        try:
            update_review_hud(player_data, current_skill)
        except Exception:
            pass
    except Exception:
        pass


def _refresh_skill_availability() -> None:
    """Recompute and refresh Smithing/Crafting availability in the menu."""
    try:
        can_craft_any = any(
            can_craft_item_pure(
                player_data.get("crafting_level", 1),
                player_data.get("inventory", {}),
                item_name,
                CRAFTING_DATA,
            )
            for item_name in CRAFTING_DATA.keys()
        )
        refresh_skill_availability(can_smelt_any_bar(), can_craft_any)
    except Exception:
        pass


def show_skill_selection():
    global current_skill
    selected = ui.show_skill_selection_dialog(current_skill, can_smelt_any_bar())
    if selected is None:
        return
    save_skill(selected, None)

def save_skill(skill, dialog):
    global current_skill
    if skill == "Smithing" and not can_smelt_any_bar():
        show_error_message("No Ores Available", "You don't have enough ores to smelt any bars. Mine some ores first!")
    else:
        current_skill = skill
        ui.update_menu_visibility(current_skill)
        # Persist immediately so the selection survives window close
        try:
            mw.col.set_config("ankiscape_current_skill", current_skill)
        except Exception:
            pass
        # Update the HUD immediately so users see the new skill progress without waiting for XP
        try:
            update_review_hud(player_data, current_skill)
        except Exception:
            pass
        if dialog:
            dialog.accept()

## menu visibility now handled by ui.update_menu_visibility

## show_achievement_dialog provided by ui.py

## show_level_up_dialog provided by ui.py

def show_craft_selection():
    selected = ui.show_craft_selection_dialog(
        current_craft=player_data.get("current_craft", ""),
        crafting_level=player_data.get("crafting_level", 1),
        inventory=player_data.get("inventory", {}),
        CRAFTING_DATA=CRAFTING_DATA,
        CRAFTED_ITEM_IMAGES=CRAFTED_ITEM_IMAGES,
    )
    if selected:
        player_data["current_craft"] = selected
        save_player_data()

def has_crafting_materials(item):
    return has_crafting_materials_pure(item, player_data["inventory"], CRAFTING_DATA)

def on_crafting_answer():
    item = player_data["current_craft"]

    # Check level and material requirements first
    if not has_crafting_materials(item):
        show_error_message("Insufficient materials", f"You don't have enough materials to craft {item}.")
        return

    # Apply crafting via pure function (handles Soft clay and crafted items)
    new_inv, exp_gained, ok = apply_crafting_pure(item, player_data["inventory"], CRAFTING_DATA)
    if not ok:
        show_error_message("Insufficient materials", f"You don't have enough materials to craft {item}.")
        return

    # Update player data and UI
    player_data["inventory"] = new_inv
    player_data["crafting_exp"] += exp_gained
    level_up_check("Crafting", player_data)
    check_achievements(player_data)
    save_player_data()

    # Refresh availability for Crafting/Smithing in the open menu (enables, never auto-selects)
    try:
        can_craft_any = any(
            can_craft_item_pure(player_data.get("crafting_level", 1), player_data.get("inventory", {}), item_name, CRAFTING_DATA)
            for item_name in CRAFTING_DATA.keys()
        )
        refresh_skill_availability(can_smelt_any_bar(), can_craft_any)
    except Exception:
        pass

    _refresh_skill_availability()
    _show_exp(exp_gained)

def show_bar_selection():
    selected = ui.show_bar_selection_dialog(
        current_bar=player_data.get("current_bar", "Bronze bar"),
        smithing_level=player_data.get("smithing_level", 1),
        BAR_DATA=BAR_DATA,
        BAR_IMAGES=BAR_IMAGES,
    )
    if selected:
        player_data["current_bar"] = selected
        save_player_data()


def show_tree_selection():
    selected = show_tree_selection_dialog(
        current_tree=player_data.get("current_tree", ""),
        woodcutting_level=player_data.get("woodcutting_level", 1),
        TREE_DATA=TREE_DATA,
        TREE_IMAGES=TREE_IMAGES,
    )
    if selected:
        player_data["current_tree"] = selected
        save_player_data()

def show_ore_selection():
    selected = show_ore_selection_dialog(
        current_ore=player_data.get("current_ore", "Rune essence"),
        mining_level=player_data.get("mining_level", 1),
        ORE_DATA=ORE_DATA,
        ORE_IMAGES=ORE_IMAGES,
    )
    if selected:
        player_data["current_ore"] = selected
        save_player_data()



def _on_main_menu():
    def _set_floating_enabled(val: bool):
        try:
            mw.col.set_config("ankiscape_floating_enabled", bool(val))
        except Exception:
            pass
        # Re-inject on current screens for immediate effect
        try:
            _inject_reviewer_floating_button()
        except Exception:
            pass
        try:
            _inject_overview_floating_button()
        except Exception:
            pass

    def _set_floating_position(pos: str):
        try:
            if pos not in ("left", "right"):
                pos = "right"
            mw.col.set_config("ankiscape_floating_position", pos)
        except Exception:
            pass
        try:
            _inject_reviewer_floating_button()
        except Exception:
            pass
        try:
            _inject_overview_floating_button()
        except Exception:
            pass

    ui.show_main_menu(
        player_data,
        current_skill,
        can_smelt_any_bar(),
        on_save_skill=lambda skill: save_skill(skill, None),
        on_set_ore=lambda ore: _set_value("current_ore", ore),
        on_set_tree=lambda tree: _set_value("current_tree", tree),
        on_set_bar=lambda bar: _set_value("current_bar", bar),
        on_set_craft=lambda item: _set_value("current_craft", item),
        on_set_floating_enabled=_set_floating_enabled,
        on_set_floating_position=_set_floating_position,
        on_try_evolved=lambda: _switch_mode_now("evolved", open_onboarding=True),
    )


def _set_value(key: str, value):
    player_data[key] = value
    save_player_data()


def initialize_menu():
    debug_log("initialize_menu: creating AnkiScape menu")
    ui.create_menu(on_main_menu=runtime_menu_opener,
                   on_try_evolved=lambda: _switch_mode_now("evolved", open_onboarding=True))
    # Refresh Deck Browser so injected content becomes visible after login
    try:
        debug_log("initialize_menu: forcing deck browser refresh")
        _force_deck_browser_refresh()
    except Exception:
        debug_log("initialize_menu: deck browser refresh failed")
        pass


# Main functionality

def on_smithing_answer():
    bar = player_data["current_bar"]
    bar_spec = BAR_DATA[bar]
    player_level = player_data["smithing_level"]

    if player_level < bar_spec["level"]:
        show_error_message("Insufficient level", f"You need level {bar_spec['level']} Smithing to smelt {bar}.")
        return

    # Use pure smelt application
    new_inv, exp_gained, ok = apply_smelt_pure(bar, player_data["inventory"], BAR_DATA)
    if not ok:
        # Find first missing ore to provide a helpful message
        for ore, amount in bar_spec["ore_required"].items():
            if player_data["inventory"].get(ore, 0) < amount:
                show_error_message("Insufficient ore", f"You need {amount} {ore} to smelt {bar}.")
                break
        return

    player_data["inventory"] = new_inv
    player_data["smithing_exp"] += exp_gained
    level_up_check("Smithing", player_data)
    check_achievements(player_data)
    save_player_data()

    # Refresh availability for Crafting/Smithing in the open menu after smelting
    try:
        can_craft_any = any(
            can_craft_item_pure(player_data.get("crafting_level", 1), player_data.get("inventory", {}), item_name, CRAFTING_DATA)
            for item_name in CRAFTING_DATA.keys()
        )
        refresh_skill_availability(can_smelt_any_bar(), can_craft_any)
    except Exception:
        pass

    _refresh_skill_availability()
    _show_exp(exp_gained)
from .logic import calculate_woodcutting_probability, calculate_mining_probability


def on_woodcutting_answer():
    tree = player_data["current_tree"]
    spec = TREE_DATA[tree]
    player_level = player_data["woodcutting_level"]

    woodcutting_probability = calculate_woodcutting_probability(player_level, spec["probability"])
    r_action = random.random()
    new_inv, exp_gained, ok = apply_woodcutting_pure(tree, player_data["inventory"], TREE_DATA, r_action, woodcutting_probability)
    if ok:
        if "logs_cut_today" not in player_data:
            player_data["logs_cut_today"] = 0
        player_data["logs_cut_today"] += 1
        player_data["inventory"] = new_inv
        player_data["woodcutting_exp"] += exp_gained
        level_up_check("Woodcutting", player_data)
        check_achievements(player_data)
        save_player_data()

    _show_exp(exp_gained)


from .logic import calculate_woodcutting_probability, calculate_mining_probability


def on_good_answer():
    global current_skill, exp_awarded
    if exp_awarded:
        return
    if current_skill == "Mining":
        ore = player_data["current_ore"]
        ore_spec = ORE_DATA[ore]
        player_level = player_data["mining_level"]

        mining_probability = calculate_mining_probability(player_level, ore_spec["probability"])
        r_action = random.random()
        r_gem_chance = random.random()
        r_gem_pick = random.random()

        new_inv, exp_gained, ok, gem = apply_mining_pure(
            ore,
            player_data["inventory"],
            ORE_DATA,
            GEM_DATA,
            r_action,
            mining_probability,
            r_gem_chance,
            r_gem_pick,
            gem_drop_chance=1/256,
        )
        if ok:
            if "ores_mined_today" not in player_data:
                player_data["ores_mined_today"] = 0
            player_data["ores_mined_today"] += 1
            player_data["inventory"] = new_inv
            player_data["mining_exp"] += exp_gained
            level_up_check("Mining", player_data)
            check_achievements(player_data)
            save_player_data()

            # If the main menu is open, auto-enable Smithing/Crafting when they become possible.
            _refresh_skill_availability()
            _show_exp(exp_gained)

    elif current_skill == "Woodcutting":
        on_woodcutting_answer()

    elif current_skill == "Smithing":
        on_smithing_answer()

    elif current_skill == "Crafting":
        on_crafting_answer()

    exp_awarded = True


## Removed roll_gem wrapper; mining uses apply_mining_pure directly.


def on_answer_card(self, ease, _old):
    global card_turned, exp_awarded, answer_shown
    # Routing (3.0): this wrapper solely dispatches existing Classic behavior.
    # In Evolved it passes through unchanged; Evolved credits from
    # reviewer_did_answer_card after Anki accepts the answer.
    try:
        from . import runtime as _rt_mod
        _rt = _rt_mod.get_runtime()
        if _rt.profile_loaded and getattr(_rt.active_adapter, "name", "") == "evolved":
            card_turned = False
            answer_shown = False
            return _old(self, ease)
    except Exception:
        pass
    if ease > 1 and current_skill in ["Mining", "Woodcutting",
                                      "Smithing", "Crafting"] and card_turned and not exp_awarded and answer_shown:
        on_good_answer()
        exp_awarded = True
    card_turned = False
    answer_shown = False  # Reset for the next card
    return _old(self, ease)


def on_card_did_show(card):
    global card_turned, exp_awarded, answer_shown
    card_turned = True
    exp_awarded = False
    answer_shown = False
    if _evolved_on_question_hook(card):
        return
    # Classic path: legacy HUD.
    try:
        ensure_review_hud()
        update_review_hud(player_data, current_skill)
    except Exception:
        pass


def _evolved_on_question_hook(card=None):
    """Evolved: start/continue session + refresh the anchored HUD."""
    try:
        if not _RUNTIME_AVAILABLE:
            return False
        rt = _runtime_mod.get_runtime()
        if not rt.profile_loaded or getattr(rt.active_adapter, "name", "") != "evolved":
            return False
        _evolved_session_on_question(card)
        try:
            web = getattr(getattr(mw, "reviewer", None), "web", None)
            if web is not None:
                web._ankiscape_completion_checked = False
        except Exception:
            pass
        try:
            from .evolved.ui import qt_hud
            settings = _evolved_get_settings()
            qt_hud.update_hud(
                mw, _evolved_projection(),
                _evolved_skill_selection(),
                {"visible": bool(settings.get("hud_visible", True)),
                 "position": settings.get("hud_position", "bottom"),
                 "ui_scale": settings.get("ui_scale", 100),
                 "reduced_motion": bool(settings.get("reduced_motion", False))})
        except Exception as exc:
            _evolved_note_shell_error(f"hud:{exc!r}")
        return True
    except Exception as exc:
        _evolved_note_shell_error(f"question_hook:{exc!r}")
        return False


def _evolved_on_state_change(new_state, _old_state=None):
    """Hide the Evolved HUD whenever Anki leaves the reviewer."""
    try:
        if _RUNTIME_AVAILABLE and _runtime_mod.get_runtime().profile_loaded:
            rt = _runtime_mod.get_runtime()
            if getattr(rt.active_adapter, "name", "") == "evolved" and new_state != "review":
                from .evolved.ui import qt_hud
                qt_hud.hide_hud(mw)
    except Exception:
        pass


def on_show_answer(reviewer):
    global answer_shown
    answer_shown = True
    if not _classic_hud_allowed():
        return
    # Keep the Classic HUD in sync when flipping.
    try:
        update_review_hud(player_data, current_skill)
    except Exception:
        pass


## show_error_message now provided by ui.show_error_message


def can_smelt_any_bar():
    return can_smelt_any_bar_pure(player_data["inventory"], player_data["smithing_level"], BAR_DATA)

def create_soft_clay():
    new_inv, ok = create_soft_clay_pure(player_data["inventory"]) 
    if ok:
        player_data["inventory"] = new_inv
    return ok

# Removed legacy safe_deduct_from_inventory; use utils.safe_deduct_from_inventory where needed.

# Initialization and hooks
def initialize_exp_popup():
    mw.exp_popup = ExpPopup(mw)


# Flexible wrappers to handle version differences in hook signatures
def _classic_hud_allowed() -> bool:
    try:
        if not _RUNTIME_AVAILABLE:
            return True
        rt = _runtime_mod.get_runtime()
        if rt.profile_loaded and getattr(rt.active_adapter, "name", "") == "evolved":
            return False
    except Exception:
        pass
    return True


def _on_rev_show_question(*_args, **_kwargs):
    _inject_reviewer_floating_button()
    if not _classic_hud_allowed():
        return
    try:
        from .ui import get_config_bool  # type: ignore
        if get_config_bool("ankiscape_review_hud_enabled", True):
            ensure_review_hud()
            update_review_hud(player_data, current_skill)
    except Exception:
        pass

def _on_rev_show_answer(*_args, **_kwargs):
    _inject_reviewer_floating_button()
    if not _classic_hud_allowed():
        return
    try:
        from .ui import get_config_bool  # type: ignore
        if get_config_bool("ankiscape_review_hud_enabled", True):
            ensure_review_hud()
            update_review_hud(player_data, current_skill)
    except Exception:
        pass

# Ensure the floating button is injected on the deck Overview as it refreshes
def _on_overview_did_refresh(overview):
    try:
        _inject_overview_floating_button(overview)
    except Exception:
        pass
    # Hide HUD off the review screen
    try:
        hide_review_hud()
    except Exception:
        pass

# Centralized hook registration
try:
    from . import hooks as _hooks
    _hooks.register_hooks(
        {
            "profile_loaded": [
                load_player_data,
                initialize_exp_popup,
                initialize_skill,
                _initialize_debug_from_config,
                migrate_legacy_settings,
                initialize_menu,
                (lambda: _register_deck_browser_button()),
            ],
            "reviewer_question": [on_card_did_show, _on_rev_show_question],
            "reviewer_answer": [on_card_did_show, on_show_answer, _on_rev_show_answer],
            "answer_wrapper": on_answer_card,
        }
    )
    # Overview: inject after refresh so the icon is always present on the Study Now screen
    try:
        try:
            gui_hooks.overview_did_refresh.remove(_on_overview_did_refresh)  # type: ignore[attr-defined]
        except Exception:
            pass
        gui_hooks.overview_did_refresh.append(_on_overview_did_refresh)  # type: ignore[attr-defined]
    except Exception:
        # Fallback for environments without overview_did_refresh: defer after will_refresh
        try:
            from aqt.qt import QTimer  # type: ignore
        except Exception:
            QTimer = None  # type: ignore
        def _on_overview_will_refresh(overview):
            if QTimer is not None:
                try:
                    QTimer.singleShot(0, lambda: _inject_overview_floating_button(overview))
                except Exception:
                    pass
            else:
                try:
                    _inject_overview_floating_button(overview)
                except Exception:
                    pass
        try:
            try:
                gui_hooks.overview_will_refresh.remove(_on_overview_will_refresh)  # type: ignore[attr-defined]
            except Exception:
                pass
            gui_hooks.overview_will_refresh.append(_on_overview_will_refresh)  # type: ignore[attr-defined]
        except Exception:
            pass
except Exception:
    # Fallback: in case hooks module import fails, keep behavior by direct registration
    try:
        addHook("profileLoaded", load_player_data)
        addHook("profileLoaded", initialize_exp_popup)
        addHook("profileLoaded", initialize_skill)
        addHook("profileLoaded", migrate_legacy_settings)
        addHook("profileLoaded", initialize_menu)
        addHook("profileLoaded", lambda: _register_deck_browser_button())
    except Exception:
        pass
    try:
        gui_hooks.reviewer_did_show_question.append(on_card_did_show)
        gui_hooks.reviewer_did_show_answer.append(on_card_did_show)
        gui_hooks.reviewer_did_show_answer.append(on_show_answer)
        gui_hooks.reviewer_did_show_question.append(_on_rev_show_question)
        gui_hooks.reviewer_did_show_answer.append(_on_rev_show_answer)
        Reviewer._answerCard = wrap(Reviewer._answerCard, on_answer_card, "around")
        # Overview: inject after refresh so the icon is always present on the Study Now screen
        try:
            try:
                gui_hooks.overview_did_refresh.remove(_on_overview_did_refresh)  # type: ignore[attr-defined]
            except Exception:
                pass
            gui_hooks.overview_did_refresh.append(_on_overview_did_refresh)  # type: ignore[attr-defined]
        except Exception:
            pass
    except Exception:
        pass

# Menu is created on profile load via initialize_menu

    # --- Handle JS bridge messages from injected buttons ---
    def _on_js_message(handled, message, context):  # type: ignore[no-redef]
        """Respond to messages sent via pycmd() in injected JS."""
        try:
            if isinstance(message, str):
                if message == "ankiscape_open_menu":
                    debug_log("bridge: ankiscape_open_menu received")
                    global _LAST_MENU_OPEN_TS
                    now = time.time()
                    if is_main_menu_open():
                        debug_log("bridge: menu already open; focusing")
                        try:
                            focus_main_menu_if_open()
                        except Exception:
                            pass
                    elif now - _LAST_MENU_OPEN_TS > 0.4:  # debounce
                        _LAST_MENU_OPEN_TS = now
                        debug_log("bridge: opening main menu via _on_main_menu")
                        try:
                            try:
                                from aqt.qt import QTimer  # type: ignore
                            except Exception:
                                QTimer = None  # type: ignore
                            if QTimer is not None:
                                QTimer.singleShot(0, _on_main_menu)
                                debug_log("bridge: scheduled _on_main_menu with QTimer")
                            else:
                                _on_main_menu()
                                debug_log("bridge: called _on_main_menu directly (no QTimer)")
                        except Exception:
                            debug_log("bridge: _on_main_menu raised; swallowed")
                            pass
                    return (True, message)
                if message.startswith("ankiscape_log:"):
                    try:
                        debug_log(f"js: {message[len('ankiscape_log:'):]}")
                    except Exception:
                        pass
                    # Not handled; allow default processing to continue
                    return (handled, message)
                # Hardening: do not intercept native Anki navigation messages
                try:
                    low = message.lower()
                except Exception:
                    low = ""
                if (
                    low.startswith("open:")
                    or low in ("decks", "add", "browse", "stats", "sync")
                    or low == "study" or low == "review" or low == "start"
                    or low.startswith("study") or low.startswith("review") or low.startswith("start")
                    or low in ("preview", "previewer", "card-info", "addcards")
                ):
                    return (False, message)
        except Exception:
            debug_log("bridge: exception in _on_js_message")
            pass
        # Default: do not intercept messages we don't recognize
        try:
            if isinstance(message, str):
                return (False, message)
        except Exception:
            pass
        return (handled, message)

    # Note: JS bridge hook is registered in injectors.register_deck_browser_button
    # to keep one consistent handler and predictable order.

# --- Deck Browser bottom button integration ---


def _force_deck_browser_refresh():
    """Trigger a Deck Browser rerender so injected content becomes visible immediately."""
    try:
        db = getattr(mw, "deckBrowser", None)
        if db is None:
            debug_log("force_refresh: no deckBrowser present")
            return
        # Prefer refresh when available, otherwise renderPage
        if hasattr(db, "refresh"):
            debug_log("force_refresh: calling deckBrowser.refresh()")
            db.refresh()
        elif hasattr(db, "renderPage"):
            debug_log("force_refresh: calling deckBrowser.renderPage()")
            db.renderPage()
        else:
            debug_log("force_refresh: deckBrowser has no refresh or renderPage")
    except Exception:
        debug_log("force_refresh: failed to refresh")
        pass


# --- 3.0 mode routing (contract A; additive, Classic behavior preserved) ---
# Root hooks above are installed once at import. This section begins/ends the
# per-profile Runtime without reading a collection at import time. Every new
# Evolved callback routes through the Runtime; Classic keeps its legacy path.
try:
    from . import mode as _mode_mod
    from . import runtime as _runtime_mod
    _RUNTIME_AVAILABLE = True
except Exception:
    _mode_mod = None  # type: ignore
    _runtime_mod = None  # type: ignore
    _RUNTIME_AVAILABLE = False

_EVOLVED_CTX: dict = {"engine": None, "journal": None, "game_uuid": None,
                       "generation": 0, "user_id": None, "profile_session": None,
                       "sync_service": None, "endpoint": None,
                       "endpoint_dev": None, "self_context": None}

# Async reward finalization: review_key -> captured context. Outcomes are
# finalized exactly once for the session recap and the 200-review sync
# trigger; Undo before completion drops the pending entry (no obsolete toast).
_PENDING_REVIEWS: dict = {}
_PENDING_POLLS = {"count": 0}
# Persistent nonmodal game-recovery state after a failed interactive write.
_RECOVERY_WARNING: dict = {"active": False, "message": "", "since": 0.0}


def _evolved_mark_recovery(reason) -> None:
    """A failed interactive write: keep reviewing, warn persistently, award
    nothing and never claim the credit was saved."""
    _RECOVERY_WARNING.update({"active": True, "message": str(reason)[:200],
                              "since": time.time()})
    try:
        from .evolved.ui.shell import refresh_shell
        if getattr(mw, "ankiscape_evolved_shell", None) is not None:
            refresh_shell(mw)
    except Exception:
        pass


def _evolved_clear_recovery() -> None:
    if _RECOVERY_WARNING.get("active"):
        _RECOVERY_WARNING.update({"active": False, "message": "", "since": 0.0})


def runtime_menu_opener():
    """Preferred menu entry for injector bridges: routes via active adapter."""
    try:
        if _RUNTIME_AVAILABLE:
            rt = _runtime_mod.get_runtime()
            if rt.profile_loaded and getattr(rt.active_adapter, "name", "") == "evolved":
                if _open_evolved_shell():
                    return
                # No Classic fallback after an Evolved error: show a
                # recoverable game error instead of the wrong game.
                _show_evolved_error("The Evolved window failed to open.")
                return
    except Exception:
        pass
    return _on_main_menu()


def _open_evolved_shell() -> bool:
    """Open/focus the singleton Evolved window. No Classic fallback."""
    try:
        from .evolved.ui import shell as _shell_mod
        from .evolved.ui.shell import show_shell
    except Exception as exc:
        _evolved_note_shell_error(repr(exc))
        return False
    try:
        if getattr(mw, "col", None) is None:
            _evolved_note_shell_error("no collection")
            return False
        deps = _evolved_shell_deps()
        ok = bool(show_shell(mw, deps))
        if not ok:
            _evolved_note_shell_error(_shell_mod.last_error() or "show_shell returned False")
        return ok
    except Exception as exc:
        _evolved_note_shell_error(repr(exc))
        return False


def _evolved_note_shell_error(message: str) -> None:
    try:
        col = getattr(mw, "col", None)
        if col is not None:
            col.set_config("ankiscape_shell_error", str(message)[:500],
                           undoable=False)
    except Exception:
        pass


# Back-compat name: every existing caller keeps working.
def _open_evolved_menu() -> bool:
    return _open_evolved_shell()


def _show_evolved_error(message: str) -> None:
    """Recoverable Evolved error; never falls back to the Classic menu."""
    try:
        from aqt.utils import showWarning
        showWarning("AnkiScape Evolved could not open.\n\n"
                    f"{message}\n\nYour progress is safe. Open AnkiScape from "
                    "the menu again after restarting Anki if this repeats.")
    except Exception:
        pass


def _evolved_rules():
    from .evolved.data import load_rules
    try:
        return load_rules()
    except Exception:
        return {}


def _evolved_projection():
    """Cached projection for the open shell/HUD. Never auto-creates a game."""
    engine = _EVOLVED_CTX.get("engine")
    if engine is None:
        if _evolved_onboarding_active() or not _evolved_identity_active():
            return {"levels": {}, "xp_micro": {}, "inventory": {},
                    "achievements": [], "counters": {}}
        try:
            engine = _ensure_evolved_engine()
        except Exception:
            engine = None
    if engine is None:
        return {"levels": {}, "xp_micro": {}, "inventory": {},
                "achievements": [], "counters": {}}
    try:
        fn = getattr(engine, "projection", None)
        if callable(fn):
            return fn()
        from .evolved.reducer import replay as _replay
        return _replay(engine._all_ops(), engine.cfg.rules, engine.cfg.game_uuid)
    except Exception:
        return {"levels": {}, "xp_micro": {}, "inventory": {},
                "achievements": [], "counters": {}}


def _evolved_identity_active() -> bool:
    try:
        from .evolved import onboarding as _onb
        col = getattr(mw, "col", None)
        if col is None:
            return False
        return _onb.identity_active(
            _onb.evolved_identity(lambda k, d=None: col.get_config(k, d)))
    except Exception:
        return False


def _evolved_selections() -> dict:
    out = {}
    try:
        col = getattr(mw, "col", None)
        if col is None:
            return out
        for skill in ("mining", "woodcutting", "smithing", "crafting",
                      "fishing", "cooking"):
            try:
                out[skill] = col.get_config(
                    f"ankiscape_evolved_current_{skill}", "") or ""
            except Exception:
                out[skill] = ""
    except Exception:
        pass
    return out


def _evolved_status() -> dict:
    sess = _evolved_profile_session()
    svc = _EVOLVED_CTX.get("sync_service")
    status = {
        "logged_in": bool(sess is not None and sess.logged_in),
        "pending": _evolved_pending(),
        "last_success": None,
        "last_error": "",
        "updating": False,
    }
    try:
        if svc is not None:
            status["last_success"] = svc.job.state.last_success
            last_error = str(svc.job.state.last_error or "")
            if "server_update_required" in last_error:
                last_error = "Server update required"
            status["last_error"] = last_error
    except Exception:
        pass
    try:
        engine = _EVOLVED_CTX.get("engine")
        if engine is not None and hasattr(engine, "projection_status"):
            projection_status = engine.projection_status()
            status["updating"] = bool(projection_status.get("busy")
                                      or projection_status.get("loading"))
            if projection_status.get("failed") and not status["last_error"]:
                status["last_error"] = f"projection:{projection_status['failed']}"
    except Exception:
        pass
    if _RECOVERY_WARNING.get("active"):
        status["recovery"] = True
        status["recovery_message"] = str(_RECOVERY_WARNING.get("message", ""))
    return status


def _evolved_account_info() -> dict:
    sess = _evolved_profile_session()
    logged_in = bool(sess is not None and sess.logged_in)
    return {"logged_in": logged_in,
            "username": (getattr(sess, "username", None) if sess else None) or "",
            "remembered": bool(sess and sess.remember and sess.persistence_ok),
            "vault_available": bool(sess and sess.vault and sess.vault.available),
            "is_test": bool(logged_in and _evolved_is_test_cohort())}


def _evolved_change_training(skill: str, resource: str) -> dict:
    """Commit skill+resource together (Train). Preview never calls this."""
    try:
        skill = str(skill or "").lower()
        if skill not in ("mining", "woodcutting", "smithing", "crafting",
                         "fishing", "cooking"):
            return {"ok": False, "error": "Unknown skill."}
        resource = str(resource or "")
        if not resource:
            return {"ok": False, "error": "Pick a resource first."}
        rules = _evolved_rules()
        from .evolved import onboarding as _onb
        if skill in _onb.GATHERING_SKILLS:
            if not _onb.validate_resource(rules, skill, resource):
                return {"ok": False, "error": "Unknown resource."}
        else:
            table = {"smithing": rules.get("bars", []),
                     "crafting": rules.get("crafting", []),
                     "cooking": rules.get("fish", [])}.get(skill, [])
            if not any(str(e.get("display")) == resource for e in table):
                return {"ok": False, "error": "Unknown resource."}
        col = getattr(mw, "col", None)
        if col is None:
            return {"ok": False, "error": "No collection open."}
        col.set_config(f"ankiscape_evolved_current_{skill}", resource)
        col.set_config("ankiscape_evolved_current_skill", skill)
        debug_log(f"evolved: training set {skill}/{resource}")
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)[:200]}


def _evolved_return_to_anki() -> None:
    """Hide the window; never picks a deck, answers a card, or goes online."""
    try:
        shell = getattr(mw, "ankiscape_evolved_shell", None)
        if shell is not None:
            shell.hide()
    except Exception:
        pass
    _evolved_focus_anki()


def _evolved_start_studying() -> None:
    """Completion action for onboarding: hide + focus Anki (same rules)."""
    _evolved_return_to_anki()


def _evolved_focus_anki() -> None:
    try:
        if getattr(mw, "state", "") == "review":
            return
        col = getattr(mw, "col", None)
        decks = getattr(col, "decks", None) if col is not None else None
        current_id = None
        try:
            current = decks.current() if decks is not None else None
            current_id = current.get("id") if isinstance(current, dict) else None
        except Exception:
            current_id = None
        if current_id:
            mw.moveToState("overview")
        else:
            mw.moveToState("deckBrowser")
    except Exception:
        pass


def _evolved_current_preset() -> str:
    try:
        from .evolved.presets import current_preset
        col = getattr(mw, "col", None)
        if col is not None:
            return current_preset(col)
    except Exception:
        pass
    return "mining"


def _evolved_on_preset(skill: str) -> dict:
    try:
        from .evolved.presets import apply_preset
        engine = _evolved_engine_if_active()
        col = getattr(mw, "col", None)
        if engine is not None and col is not None:
            return apply_preset(engine, engine.journal, col, skill)
        from .evolved.ui.menu_model import validate_preset
        if col is not None:
            col.set_config("ankiscape_evolved_catchup_preset",
                           validate_preset(skill))
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)[:200]}


def _evolved_engine_if_active():
    """The bound engine for an active, completed Evolved game (else None)."""
    engine = _EVOLVED_CTX.get("engine")
    if engine is not None:
        return engine
    if _evolved_onboarding_active() or not _evolved_identity_active():
        return None
    try:
        return _ensure_evolved_engine()
    except Exception:
        return None


# --- Settings -------------------------------------------------------------

_SETTING_KEYS = {
    "ui_scale": ("ankiscape_evolved_ui_scale", 100),
    "hud_visible": ("ankiscape_evolved_hud_visible", True),
    "hud_position": ("ankiscape_evolved_hud_position", "bottom"),
    "celebrations": ("ankiscape_evolved_celebrations", True),
    "reduced_motion": ("ankiscape_evolved_reduced_motion", False),
    "sound": ("ankiscape_evolved_sound", False),
}


def _evolved_get_settings() -> dict:
    out = {}
    col = getattr(mw, "col", None)
    for key, (cfg_key, default) in _SETTING_KEYS.items():
        value = default
        try:
            if col is not None:
                value = col.get_config(cfg_key, default)
        except Exception:
            value = default
        out[key] = value
    try:
        from .evolved.ui.theme import clamp_scale
        out["ui_scale"] = clamp_scale(out.get("ui_scale", 100))
    except Exception:
        pass
    if out.get("hud_position") not in ("top", "bottom"):
        out["hud_position"] = "bottom"
    return out


def _evolved_apply_setting(key: str, value) -> dict:
    spec = _SETTING_KEYS.get(str(key))
    if spec is None:
        return {"ok": False, "error": "unknown setting"}
    cfg_key, _default = spec
    try:
        col = getattr(mw, "col", None)
        if col is not None:
            col.set_config(cfg_key, value)
    except Exception:
        pass
    try:
        if key == "ui_scale":
            shell = getattr(mw, "ankiscape_evolved_shell", None)
            if shell is not None:
                shell.apply_scale(int(value))
        if key in ("hud_visible", "hud_position"):
            _evolved_hud_apply_settings()
    except Exception:
        pass
    return {"ok": True}


def _evolved_hud_apply_settings() -> None:
    try:
        from .evolved.ui import qt_hud
        qt_hud.apply_settings(getattr(mw, "ankiscape_evolved_hud", None),
                              {**_evolved_get_settings(),
                               "position": _evolved_get_settings().get("hud_position", "bottom")}, mw=mw)
    except Exception:
        pass


# --- Onboarding -----------------------------------------------------------

def _evolved_onboarding_state():
    from .evolved import onboarding as _onb
    state = _EVOLVED_CTX.get("onboarding")
    if state is None:
        col = getattr(mw, "col", None)
        if col is not None:
            state = _onb.load(lambda k, d=None: col.get_config(k, d))
        else:
            state = _onb.OnboardingState()
        _EVOLVED_CTX["onboarding"] = state
    return state


def _evolved_onboarding_active() -> bool:
    """True while setup is incomplete for the active Evolved profile."""
    try:
        if not _RUNTIME_AVAILABLE:
            return False
        rt = _runtime_mod.get_runtime()
        if not rt.profile_loaded:
            return False
        if getattr(rt.active_adapter, "name", "") != "evolved":
            return False
        state = _EVOLVED_CTX.get("onboarding")
        if state is None:
            return False
        return not bool(state.complete)
    except Exception:
        return False


def _evolved_onboarding_dict() -> dict:
    return _evolved_onboarding_state().to_dict()


def _evolved_onboarding_save(payload: dict) -> dict:
    from .evolved import onboarding as _onb
    state = _onb.from_dict(payload)
    col = getattr(mw, "col", None)
    if col is None:
        return {"ok": False, "error": "no collection"}
    _EVOLVED_CTX["onboarding"] = state
    return _onb.save(lambda k, v: col.set_config(k, v), state)


def _evolved_onboarding_advance() -> dict:
    from .evolved import onboarding as _onb
    state = _evolved_onboarding_state()
    state = _onb.advance(state, _evolved_rules())
    col = getattr(mw, "col", None)
    if col is not None:
        _onb.save(lambda k, v: col.set_config(k, v), state)
    return state.to_dict()


def _evolved_onboarding_back() -> dict:
    from .evolved import onboarding as _onb
    state = _onb.back(_evolved_onboarding_state())
    col = getattr(mw, "col", None)
    if col is not None:
        _onb.save(lambda k, v: col.set_config(k, v), state)
    return state.to_dict()


def _evolved_onboarding_commit(skill: str, resource: str) -> dict:
    """Atomic setup commit: activation stamp + training selection + mode."""
    try:
        import time as _time
        import uuid as _uuid
        from .evolved import onboarding as _onb
        rules = _evolved_rules()
        if not _onb.validate_resource(rules, skill, resource):
            return {"ok": False, "error": "Choose a starting resource first."}
        col = getattr(mw, "col", None)
        if col is None:
            return {"ok": False, "error": "No collection open."}
        pointer = _onb.evolved_identity(lambda k, d=None: col.get_config(k, d))
        now = int(_time.time())
        if not pointer:
            pointer = {"version": 1, "game_uuid": str(_uuid.uuid4()),
                       "activated_at": now, "snapshot_revision": 0,
                       "preset": {"skill": "mining", "effective_ts": now}}
        elif not _onb.identity_active(pointer):
            pointer["activated_at"] = now
        col.set_config("ankiscape_evolved_player_data", pointer, undoable=False)
        col.set_config(f"ankiscape_evolved_current_{skill}", resource)
        col.set_config("ankiscape_evolved_current_skill", skill)
        _mode_mod.set_requested_mode(
            lambda k, v: col.set_config(k, v), _mode_mod.EVOLVED)
        state = _evolved_onboarding_state()
        state.gathering_skill = skill
        state.starting_resource = resource
        state.step = "done"
        state.complete = True
        _onb.save(lambda k, v: col.set_config(k, v), state)
        _EVOLVED_CTX["onboarding"] = state
        engine = _ensure_evolved_engine()
        _evolved_mark_seen_levels(reset=True)
        try:
            from .evolved.catchup import run_catchup
            if engine is not None:
                run_catchup(col, engine, engine.journal, full=False)
        except Exception:
            pass
        debug_log(f"evolved: onboarding committed {skill}/{resource}")
        return {"ok": True, "game_uuid": pointer.get("game_uuid", "")}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)[:300]}


# --- Unlock notices -------------------------------------------------------

def _evolved_seen_levels() -> dict:
    try:
        col = getattr(mw, "col", None)
        if col is not None:
            raw = col.get_config("ankiscape_evolved_seen_levels", None)
            if isinstance(raw, dict):
                return {str(k): int(v) for k, v in raw.items()}
    except Exception:
        pass
    return {}


def _evolved_mark_seen_levels(*, reset: bool = False) -> None:
    try:
        col = getattr(mw, "col", None)
        if col is None:
            return
        if reset:
            col.set_config("ankiscape_evolved_seen_levels", {}, undoable=False)
            return
        projection = _evolved_projection()
        levels = projection.get("levels", {}) or {}
        col.set_config("ankiscape_evolved_seen_levels",
                       {k: int(v) for k, v in levels.items()}, undoable=False)
    except Exception:
        pass


def _evolved_unlock_notice():
    """First newly unlocked resource since the last dismissal, else None."""
    try:
        rules = _evolved_rules()
        projection = _evolved_projection()
        levels = projection.get("levels", {}) or {}
        seen = _evolved_seen_levels()
        tables = {"mining": (rules.get("ores", []), "level"),
                  "woodcutting": (rules.get("trees", []), "level"),
                  "fishing": (rules.get("fish", []), "fishing_level")}
        for skill, (entries, level_key) in tables.items():
            level = int(levels.get(skill, 1))
            seen_level = int(seen.get(skill, 1))
            for entry in sorted(entries, key=lambda e: int(e.get("tier", 0))):
                req = int(entry.get(level_key, 1))
                if seen_level < req <= level:
                    return {"skill": skill, "display": entry.get("display", ""),
                            "level": req}
    except Exception:
        pass
    return None


def _evolved_dismiss_unlock() -> None:
    _evolved_mark_seen_levels()


def _evolved_preselect_skill(skill: str) -> None:
    pending = dict(_EVOLVED_CTX.get("preselect") or {})
    pending["skill"] = str(skill or "")
    _EVOLVED_CTX["preselect"] = pending


def _evolved_preselect_resource(display: str) -> None:
    pending = dict(_EVOLVED_CTX.get("preselect") or {})
    pending["resource"] = str(display or "")
    _EVOLVED_CTX["preselect"] = pending


def _evolved_take_preselect():
    pending = _EVOLVED_CTX.pop("preselect", None)
    return pending


def _evolved_refresh_views() -> None:
    try:
        engine = _EVOLVED_CTX.get("engine")
        if engine is not None and hasattr(engine, "invalidate_projection"):
            engine.invalidate_projection()
    except Exception:
        pass
    try:
        from .evolved.ui.shell import refresh_shell
        if getattr(mw, "ankiscape_evolved_shell", None) is not None:
            refresh_shell(mw)
    except Exception:
        pass
    try:
        _evolved_update_hud_after_reward()
    except Exception:
        pass


def _evolved_update_hud_after_reward() -> None:
    try:
        from .evolved.ui import qt_hud
        engine = _EVOLVED_CTX.get("engine")
        if engine is None:
            return
        projection = _evolved_projection()
        skill = _evolved_skill_selection()
        settings = _evolved_get_settings()
        qt_hud.update_hud(
            mw, projection, skill,
            {"visible": bool(settings.get("hud_visible", True)),
             "position": settings.get("hud_position", "bottom"),
             "ui_scale": settings.get("ui_scale", 100),
             "reduced_motion": bool(settings.get("reduced_motion", False))})
    except Exception:
        pass


def _evolved_session_recap():
    try:
        from .evolved import session_summary as _ss
        return _ss.current_recap(_EVOLVED_CTX.get("session"),
                                 _evolved_projection())
    except Exception:
        return None


def _evolved_study_deck_id(card=None) -> int:
    """Deck being studied (filtered decks use their own deck id)."""
    try:
        col = getattr(mw, "col", None)
        decks = getattr(col, "decks", None) if col is not None else None
        current = decks.current() if decks is not None else None
        if isinstance(current, dict) and current.get("id"):
            return int(current["id"])
    except Exception:
        pass
    try:
        return int(getattr(card, "did", 0) or 0)
    except Exception:
        return 0


def _evolved_session_on_question(card=None) -> None:
    """Start/continue the session when a review card appears."""
    try:
        if _evolved_onboarding_active() or not _evolved_identity_active():
            return
        from .evolved import session_summary as _ss
        deck_id = _evolved_study_deck_id(card)
        session = _EVOLVED_CTX.get("session")
        if session is None or not _ss.same_deck(session, deck_id):
            _EVOLVED_CTX["session"] = _ss.start(deck_id, _evolved_projection())
    except Exception:
        pass


def _evolved_check_completion() -> None:
    """Detect Anki's deck-completion screen and show the live recap.

    Anki 26 renders completion in the main web view (no `_showCongrats`
    method); older versions are covered by the method wrap. Both paths land
    in `_evolved_on_completion`, which is idempotent per completion.
    """
    try:
        if not _RUNTIME_AVAILABLE:
            return
        rt = _runtime_mod.get_runtime()
        if not rt.profile_loaded or getattr(rt.active_adapter, "name", "") != "evolved":
            return
        reviewer = getattr(mw, "reviewer", None)
        web = getattr(reviewer, "web", None)
        if web is None:
            return
        if getattr(web, "_ankiscape_completion_checked", False):
            return
        js = ("(document.body && document.body.innerText)"
              ".indexOf('Congratulations') !== -1")
        if getattr(mw, "state", "") != "review":
            return

        def _cb(result):
            try:
                done = bool(result)
                if isinstance(result, str):
                    done = result.strip().lower() in ("true", "1")
                if done:
                    web._ankiscape_completion_checked = True
                    _evolved_on_completion()
            except Exception:
                pass

        try:
            web.evalWithCallback(js, _cb)
        except Exception:
            pass
    except Exception:
        pass


def _evolved_on_reviewer_will_end(*_args, **_kwargs):
    """Completion at the moment the reviewer ends: only an emptied queue is a
    completion; leaving early keeps the recap in Training Home without any
    popup (decision 11/23)."""
    try:
        if not _RUNTIME_AVAILABLE:
            return
        rt = _runtime_mod.get_runtime()
        if not rt.profile_loaded or getattr(rt.active_adapter, "name", "") != "evolved":
            return
        counts = None
        try:
            counts = mw.col.sched.counts()
        except Exception:
            counts = None
        if counts is None:
            return
        try:
            exhausted = all(int(c) == 0 for c in list(counts)[:3])
        except (TypeError, ValueError):
            exhausted = False
        if exhausted:
            _evolved_on_completion()
    except Exception:
        pass


def _evolved_on_completion() -> None:
    """Anki's deck-completion screen: show the live session recap.

    Idempotent redraw; a zero-review session says so without celebration.
    """
    try:
        if not _RUNTIME_AVAILABLE:
            return
        rt = _runtime_mod.get_runtime()
        if not rt.profile_loaded or getattr(rt.active_adapter, "name", "") != "evolved":
            return
        from .evolved import session_summary as _ss
        session = _EVOLVED_CTX.get("session")
        if session is None:
            session = _ss.start(_evolved_study_deck_id(), _evolved_projection())
            _EVOLVED_CTX["session"] = session
        session.completed = True
        recap = _ss.current_recap(session, _evolved_projection())
        text = (recap or {}).get("text", "")
        if recap is None:
            text = ("No reviews completed in this session — pick a deck from "
                    "the deck list and press Study Now.")
            recap = {"reviews": 0, "text": text, "completed": True}
        if session.completion_shown:
            return  # idempotent redraw: never re-inject twice for one show
        session.completion_shown = True
        try:
            mw.ankiscape_last_recap = recap
        except Exception:
            pass
        _evolved_inject_recap_html(text)
        # Anki 26 has no persistent congrats page (it returns to the deck
        # list), so the same recap is published in the nonmodal game window.
        try:
            if _open_evolved_shell():
                shell = getattr(mw, "ankiscape_evolved_shell", None)
                if shell is not None and hasattr(shell, "show_recap"):
                    shell.show_recap(recap)
        except Exception:
            pass
        try:
            from .evolved.ui.shell import refresh_shell
            if getattr(mw, "ankiscape_evolved_shell", None) is not None:
                refresh_shell(mw)
        except Exception:
            pass
    except Exception as exc:
        debug_log(f"evolved: completion recap failed: {exc!r}")


def _evolved_inject_recap_html(text: str) -> None:
    """Append the recap to the congrats webview (never card content)."""
    import html as _html
    try:
        reviewer = getattr(mw, "reviewer", None)
        view = getattr(reviewer, "web", None)
        if view is None:
            return
        safe = _html.escape(str(text))
        js = (
            "(function(){var d=document.getElementById('ankiscape-session-recap');"
            "if(!d){d=document.createElement('div');d.id='ankiscape-session-recap';"
            "d.style.cssText='margin:16px auto;padding:12px 16px;max-width:640px;"
            "background:#3A342A;border:2px solid #796744;border-radius:4px;"
            "color:#F2E6C9;font-size:14px;text-align:left;';"
            "var host=document.querySelector('#qa, .card, body')||document.body;"
            "host.appendChild(d);}"
            "d.innerText=%s;})();" % _js_string(safe)
        )
        try:
            view.eval(js)
        except Exception:
            web = getattr(view, "web_view", None)
            if web is not None and hasattr(web, "runJavaScript"):
                web.runJavaScript(js)
    except Exception:
        pass


def _js_string(value: str) -> str:
    import json as _json
    return _json.dumps(value)


def _evolved_end_session() -> None:
    session = _EVOLVED_CTX.get("session")
    if session is not None:
        try:
            from .evolved import session_summary as _ss
            _ss.close(session)
        except Exception:
            pass
    _EVOLVED_CTX["session"] = None


def _evolved_hiscores_cache_key(skill: str, cohort: bool = False) -> str:
    return ("test:" if cohort else "public:") + str(skill)


def _evolved_hiscores_cache_get(skill: str, cohort: bool = False):
    cache = _EVOLVED_CTX.get("hiscores_cache") or {}
    return cache.get(_evolved_hiscores_cache_key(skill, cohort))


def _evolved_hiscores_cache_set(skill: str, payload: dict,
                                cohort: bool = False) -> None:
    cache = dict(_EVOLVED_CTX.get("hiscores_cache") or {})
    cache[_evolved_hiscores_cache_key(skill, cohort)] = payload
    _EVOLVED_CTX["hiscores_cache"] = cache


def _evolved_is_test_cohort() -> bool:
    """Server-reported cohort for the signed-in account. Never a client
    flag: a cached self_context from /rpc/self_context, defaulting to the
    public cohort when unavailable."""
    try:
        sess = _evolved_profile_session()
        if sess is None or not sess.logged_in:
            return False
        cache = _EVOLVED_CTX.get("self_context")
        if isinstance(cache, dict) and cache.get("user_id") == sess.user_id:
            return bool(cache.get("is_test"))
        from .evolved.service import fetch_self_context
        from .evolved.net import post_json as _post
        endpoint = _evolved_endpoint()
        if endpoint is None:
            return False
        context = fetch_self_context(_post, endpoint, sess)
        if not isinstance(context, dict):
            return False
        _EVOLVED_CTX["self_context"] = {"user_id": sess.user_id, **context}
        return bool(context.get("is_test"))
    except Exception:
        return False


def _evolved_query_hiscores_async(skill: str, limit: int, on_done,
                                  cohort: bool = False) -> None:
    """Run the query off the Qt thread; deliver on the main thread guarded by
    generation + user identity."""
    import time as _time
    try:
        rt = _runtime_mod.get_runtime()
        generation = int(rt.generation or 0)
        user = _EVOLVED_CTX.get("user_id")
    except Exception:
        generation, user = 0, None

    def _task():
        rows = _evolved_query_hiscores(skill, limit, cohort)
        return {"ok": True, "rows": rows, "fetched_at": _time.time()}

    def _deliver(result):
        try:
            rt = _runtime_mod.get_runtime()
            if int(rt.generation or 0) != generation:
                return
            if (_EVOLVED_CTX.get("user_id") or None) != (user or None):
                return
        except Exception:
            return
        if isinstance(result, dict) and result.get("ok"):
            _evolved_hiscores_cache_set(skill, result, cohort)
        try:
            on_done(result)
        except Exception:
            pass

    def _failure(exc):
        if isinstance(exc, Exception):
            _deliver({"ok": False, "error": str(exc)[:200]})
        else:
            _deliver(exc)

    taskman = getattr(mw, "taskman", None)
    if taskman is not None:
        try:
            def _wrapped():
                return _task()

            def _on_bg(future_or_result):
                # Anki >= 2.1.50 passes a Future; older builds pass the value.
                try:
                    if hasattr(future_or_result, "result"):
                        value = future_or_result.result()
                    else:
                        value = future_or_result
                except Exception as exc:
                    _failure(exc)
                    return
                _deliver(value)

            taskman.run_in_background(_wrapped, _on_bg)
            return
        except Exception:
            pass
    try:
        _deliver(_task())
    except Exception as exc:
        _failure(exc)


def _evolved_lookup_async(username: str, skill: str, on_done,
                          cohort: bool = False) -> None:
    """Username lookup against public_profile + selected-skill Hiscores
    rank. Delivered on the main thread; late callbacks after a profile or
    generation change are discarded (same guard as the ranks query)."""
    try:
        rt = _runtime_mod.get_runtime()
        generation = int(rt.generation or 0)
        user = _EVOLVED_CTX.get("user_id")
    except Exception:
        generation, user = 0, None

    def _task():
        from .evolved.net import post_json as _post
        from .evolved.service import query_public_profile
        sess = _EVOLVED_CTX.get("profile_session")
        endpoint = _evolved_endpoint()
        if endpoint is None:
            return {"ok": False, "error": "unconfigured"}
        selected = str(skill or "").lower() or _evolved_skill_selection()
        try:
            return query_public_profile(_post, endpoint, sess,
                                        username=username, skill=selected,
                                        limit=50, cohort=cohort)
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}

    def _deliver(result):
        try:
            rt = _runtime_mod.get_runtime()
            if int(rt.generation or 0) != generation:
                return
            if (_EVOLVED_CTX.get("user_id") or None) != (user or None):
                return
        except Exception:
            return
        try:
            on_done(result)
        except Exception:
            pass

    taskman = getattr(mw, "taskman", None)
    if taskman is not None:
        try:
            def _on_bg(value):
                try:
                    if hasattr(value, "result"):
                        value = value.result()
                except Exception as exc:
                    value = {"ok": False, "error": str(exc)[:200]}
                _deliver(value)
            taskman.run_in_background(_task, _on_bg)
            return
        except Exception:
            pass
    try:
        _deliver(_task())
    except Exception as exc:
        _deliver({"ok": False, "error": repr(exc)[:200]})


def _evolved_register_dialog_flow() -> None:
    """Create account -> email code -> linked-game result -> back to origin."""
    try:
        from .evolved.ui import dialogs as _dlg
        from .evolved import accounts as _accounts
        from .evolved.net import post_json as _post
        sess = _evolved_profile_session()
        endpoint = _evolved_endpoint()
        if sess is None or endpoint is None:
            return
        state = {"username": "", "email": ""}

        def _on_register(fields):
            state["username"] = fields.get("username", "")
            state["email"] = fields.get("email", "")
            result = _accounts.register(_post, endpoint,
                                        username=state["username"],
                                        email=state["email"],
                                        password=fields.get("password", ""))
            if not result.ok:
                return {"ok": False, "error": result.error}
            return {"ok": True}

        if not _dlg.show_register_dialog(
                getattr(mw, "app", None) and mw or None, _on_register):
            return

        def _on_code(code):
            result = _accounts.verify_code(_post, endpoint, email=state["email"],
                                           code=code, kind="signup",
                                           session=sess.session)
            if not result.ok:
                return {"ok": False, "error": result.error}
            _EVOLVED_CTX["user_id"] = sess.user_id
            try:
                svc = _evolved_sync_service()
                if svc is not None:
                    svc.maybe_sync(on_login=True)
            except Exception:
                pass
            return {"ok": True}

        result = _dlg.show_code_dialog(
            getattr(mw, "app", None) and mw or None,
            title="AnkiScape — Verify email",
            object_name="ankiscape-verify-dialog", on_submit=_on_code)
        if result:
            try:
                from aqt.utils import showInfo
                showInfo("Email verified. Your progress can sync "
                         "from Account & Sync.", parent=getattr(mw, "ankiscape_evolved_shell", None) or mw)
            except Exception:
                pass
        _evolved_refresh_views()
    except Exception:
        pass


def _evolved_logout() -> dict:
    try:
        sess = _EVOLVED_CTX.get("profile_session")
        if sess is not None:
            from .evolved import accounts as _accounts
            _accounts.logout(sess.session)
        _EVOLVED_CTX["user_id"] = None
        _EVOLVED_CTX["sync_service"] = None
        _evolved_refresh_views()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)[:200]}


def _evolved_shell_deps() -> dict:
    return {
        "ui_scale": _evolved_get_settings().get("ui_scale", 100),
        "get_rules": _evolved_rules,
        "get_projection": _evolved_projection,
        "get_status": _evolved_status,
        "get_account": _evolved_account_info,
        "get_selections": _evolved_selections,
        "get_active_skill": _evolved_skill_selection,
        "get_preset": _evolved_current_preset,
        "on_preset": _evolved_on_preset,
        "on_change_training": _evolved_change_training,
        "on_return_to_anki": _evolved_return_to_anki,
        "on_start_studying": _evolved_start_studying,
        "on_account": _evolved_account_dialog,
        "on_register": _evolved_register_dialog_flow,
        "on_recovery": _evolved_recovery_flow,
        "on_logout": _evolved_logout,
        "on_sync": _evolved_manual_sync,
        "query_hiscores": _evolved_query_hiscores,
        "query_hiscores_async": _evolved_query_hiscores_async,
        "lookup_player_async": _evolved_lookup_async,
        "get_hiscores_cache": _evolved_hiscores_cache_get,
        "on_export": _evolved_export_backup,
        "on_restore": _evolved_restore_backup,
        "on_mode_switch": _switch_mode_now,
        "get_diagnostics": _evolved_diagnostics,
        "get_settings": _evolved_get_settings,
        "apply_setting": _evolved_apply_setting,
        "onboarding_active": _evolved_onboarding_active,
        "get_onboarding": _evolved_onboarding_dict,
        "save_onboarding": _evolved_onboarding_save,
        "advance_onboarding": _evolved_onboarding_advance,
        "back_onboarding": _evolved_onboarding_back,
        "commit_onboarding": _evolved_onboarding_commit,
        "get_unlock_notice": _evolved_unlock_notice,
        "dismiss_unlock_notice": _evolved_dismiss_unlock,
        "preselect_skill": _evolved_preselect_skill,
        "preselect_resource": _evolved_preselect_resource,
        "take_preselect": _evolved_take_preselect,
        "get_session_recap": _evolved_session_recap,
        "refresh_views": _evolved_refresh_views,
        "on_report_issue": _evolved_report_issue,
    }


def _evolved_profile_session():
    """ProfileSession for this generation (created on first use, cleared on
    profile close even offline). Never reads the collection."""
    try:
        if not _RUNTIME_AVAILABLE:
            return None
        rt = _runtime_mod.get_runtime()
        gen = int(rt.generation or _EVOLVED_CTX.get("generation") or 0)
    except Exception:
        return None
    sess = _EVOLVED_CTX.get("profile_session")
    if sess is not None and getattr(sess, "generation", 0) == gen and gen:
        return sess
    try:
        from .evolved.session_store import ProfileSession
        from .evolved import accounts as _accounts
        from .evolved.net import post_json as _post
        from .evolved.credentials import CredentialVault
        endpoint = _evolved_endpoint()
        pm = getattr(mw, "pm", None)
        try:
            vault = CredentialVault(pm.profileFolder(), endpoint.base_url) if pm and endpoint else None
        except Exception:
            vault = None
        sess = ProfileSession(generation=gen, vault=vault)
        sess.bind_refresh(lambda s: _evolved_refresh_session(s, _accounts, _post))
        _EVOLVED_CTX["profile_session"] = sess
        _EVOLVED_CTX["user_id"] = sess.user_id
        return sess
    except Exception:
        return None


def _evolved_endpoint():
    """Resolve the Endpoint for this profile (dev loopback vs. prod).

    Dev mode = ANKISCAPE_DEV set (playground/E2E) → local stack with the
    anon key read live from `supabase status` (never stored). Otherwise
    prod build config; unconfigured builds get None → every network op
    reports unconfigured instead of hitting a wrong project.
    """
    import os as _os
    try:
        from .evolved.session_store import resolve_endpoint
        dev = bool(_os.environ.get("ANKISCAPE_DEV"))
        if dev:
            anon = _evolved_dev_anon_key()
            if not anon:
                return None
            return resolve_endpoint(dev=True, anon_key=anon)
        return resolve_endpoint(dev=False)
    except Exception:
        return None


def _evolved_dev_anon_key() -> str:
    """Read the local-stack anon key live (dev only; never persisted)."""
    import subprocess as _sp
    import os as _os
    try:
        root = _os.path.dirname(_os.path.abspath(__file__))
        proc = _sp.run(["supabase", "status", "-o", "env"],
                       capture_output=True, text=True, timeout=30,
                       cwd=_os.path.join(root, "server"))
        for line in (proc.stdout or "").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                if key.strip() in ("ANON_KEY", "PUBLISHABLE_KEY"):
                    return value.strip().strip('"')
    except Exception:
        pass
    return ""


def _evolved_refresh_session(session, accounts_mod, post_fn) -> bool:
    """Refresh access token via Supabase; serialized by ProfileSession."""
    try:
        endpoint = _evolved_endpoint()
        if endpoint is None or not session.refresh_token:
            return False
        from .evolved.net import NetError
        try:
            data = post_fn(endpoint, "/auth/v1/token?grant_type=refresh_token",
                           {"refresh_token": session.refresh_token})
        except NetError as exc:
            if exc.kind in ("unauthorized", "invalid", "forbidden"):
                session.clear()
            return False
        if not isinstance(data, dict):
            return False
        access, refresh = data.get("access_token"), data.get("refresh_token")
        user = data.get("user", {}) or {}
        if not access or not refresh or not user.get("id"):
            return False
        session.set(access_token=access, refresh_token=refresh,
                    user_id=user.get("id"), username=session.username)
        return True
    except Exception:
        return False


def _evolved_sync_service():
    """SyncService for this profile+game (one per game; rebuilt on switch).

    Returns None when logged out or unconfigured — callers treat that as
    offline, never as an error popup.
    """
    try:
        engine = _ensure_evolved_engine()
        if engine is None:
            return None
        sess = _evolved_profile_session()
        if sess is None or not sess.logged_in:
            return None
        endpoint = _evolved_endpoint()
        if endpoint is None:
            return None
        from .evolved.service import ServiceConfig, SyncService, make_transport
        from .evolved.net import post_json as _post
        game_uuid = engine.cfg.game_uuid
        cached = _EVOLVED_CTX.get("sync_service")
        if (cached is not None
                and _EVOLVED_CTX.get("sync_service_game") == game_uuid
                and _EVOLVED_CTX.get("sync_service_user") == sess.user_id):
            return cached
        journal = engine.journal
        cfg = ServiceConfig(endpoint=endpoint, game_uuid=game_uuid, post=_post)
        transport = make_transport(cfg, sess)
        rt = _runtime_mod.get_runtime()
        gen = int(rt.generation or 0)
        svc = SyncService(
            generation=gen, game_uuid=game_uuid, journal=journal,
            transport=transport,
            apply_remote=lambda page: _evolved_apply_remote(page),
            get_user_id=lambda: (sess.user_id if sess.logged_in else None))
        _EVOLVED_CTX["sync_service"] = svc
        _EVOLVED_CTX["sync_service_game"] = game_uuid
        _EVOLVED_CTX["sync_service_user"] = sess.user_id
        return svc
    except Exception:
        return None


def _evolved_apply_remote(page) -> None:
    """Apply downloaded ops then re-render (main thread; journal SQLite only,
    never Anki tables). The engine projection is invalidated once; the open
    shell/HUD re-read it on refresh."""
    try:
        engine = _EVOLVED_CTX.get("engine")
        if engine is not None and hasattr(engine, "invalidate_projection"):
            engine.invalidate_projection()
        try:
            from .evolved.session_summary import note_remote
            if page and page.get("operations"):
                note_remote(_EVOLVED_CTX.get("session"),
                            "Remote progress arrived in a separate update.")
        except Exception:
            pass
        from .evolved.ui.shell import refresh_shell
        if getattr(mw, "ankiscape_evolved_shell", None) is not None:
            refresh_shell(mw)
        debug_log(f"evolved: merged {len(page.get('operations', []))} remote ops")
    except Exception:
        pass


def _evolved_logged_in() -> bool:
    try:
        sess = _EVOLVED_CTX.get("profile_session")
        return bool(sess is not None and sess.logged_in)
    except Exception:
        return False


def _evolved_pending() -> int:
    try:
        engine = _EVOLVED_CTX.get("engine")
        if engine is None:
            return 0
        return int(engine.journal.count_pending_operations())
    except Exception:
        return 0


def _evolved_last_success():
    try:
        svc = _EVOLVED_CTX.get("sync_service")
        if svc is not None:
            return svc.job.state.last_success
    except Exception:
        pass
    return None


def _evolved_last_error() -> str:
    try:
        svc = _EVOLVED_CTX.get("sync_service")
        if svc is not None:
            return str(svc.job.state.last_error or "")
    except Exception:
        pass
    return ""


def _evolved_manual_sync() -> dict:
    """Manual Sync button path: run now, return structured result for the
    Hiscores status line (never a modal on failure)."""
    try:
        svc = _evolved_sync_service()
        if svc is None:
            if not _evolved_logged_in():
                return {"ok": False, "error": "logged_out_no_network"}
            return {"ok": False, "error": "sync_unconfigured"}
        return svc.force_sync()
    except Exception as exc:
        return {"ok": False, "error": repr(exc)[:200]}


def _evolved_query_hiscores(skill: str, limit: int = 50,
                            cohort: bool = False):
    try:
        from .evolved.service import query_hiscores
        from .evolved.net import post_json as _post
        sess = _EVOLVED_CTX.get("profile_session")
        endpoint = _evolved_endpoint()
        if sess is None or not sess.logged_in or endpoint is None:
            raise RuntimeError("offline — log in to sync and view hiscores")
        return query_hiscores(_post, endpoint, sess, skill=skill, limit=limit,
                              cohort=cohort)
    except Exception as exc:
        raise RuntimeError(str(exc)[:200])


def _evolved_account_dialog() -> None:
    """Account button: login/logout/recovery entry (explicit user action)."""
    try:
        from .evolved.ui import dialogs as _dlg
        from .evolved import accounts as _accounts
        from .evolved.net import post_json as _post
        sess = _evolved_profile_session()
        endpoint = _evolved_endpoint()
        if sess is None or endpoint is None:
            return
        if sess.logged_in:
            _evolved_logout()
            return
        _dlg.show_login_dialog(
            getattr(mw, "app", None) and mw or None,
            lambda fields: _evolved_login_submit(
                fields, sess, endpoint, _accounts, _post),
            on_recovery=_evolved_recovery_flow)
        _evolved_refresh_views()
    except Exception:
        pass


def _evolved_report_issue():
    """Open the private-by-default Report a bug dialog (user-driven only)."""
    try:
        from .evolved.ui.report_issue import show_report_issue
        parent = getattr(mw, "ankiscape_evolved_shell", None) or mw
        return show_report_issue(parent, {
            "collect_diagnostics": _evolved_diagnostics_payload,
            "copy_text": _evolved_copy_text,
            "open_url": _evolved_open_url,
        })
    except Exception as exc:
        return {"action": "error", "error": repr(exc)[:200]}


def _evolved_copy_text(text: str) -> bool:
    try:
        from aqt.qt import QApplication
        QApplication.clipboard().setText(str(text))
        return True
    except Exception:
        return False


def _evolved_open_url(url: str) -> bool:
    try:
        from aqt.qt import QDesktopServices, QUrl
        return bool(QDesktopServices.openUrl(QUrl(str(url))))
    except Exception:
        return False


def _evolved_runtime_versions() -> dict:
    versions = {"anki": "", "qt": ""}
    try:
        import anki as _anki
        versions["anki"] = str(getattr(_anki, "version", "") or "")
    except Exception:
        pass
    try:
        from aqt.qt import qVersion
        versions["qt"] = str(qVersion())
    except Exception:
        pass
    return versions


def _evolved_artifact_id() -> str:
    try:
        from .evolved import prod_config
        return str(getattr(prod_config, "PROD_URL", "") or "")[:24]
    except Exception:
        return ""


def _evolved_mode_name() -> str:
    try:
        if _RUNTIME_AVAILABLE:
            rt = _runtime_mod.get_runtime()
            return str(getattr(rt.active_adapter, "name", "") or "")
    except Exception:
        pass
    return ""


def _evolved_diagnostics_payload() -> dict:
    """Exact allowlisted payload a report may carry. Recorded in the bounded
    local ring; never includes raw logs, ids, paths or exception text."""
    import platform
    from .evolved import diagnostics as _diag
    from .evolved import ADDON_VERSION
    status = _evolved_status() or {}
    settings = _evolved_get_settings() or {}
    versions = _evolved_runtime_versions()
    worker: dict = {}
    try:
        engine = _EVOLVED_CTX.get("engine")
        if engine is not None and hasattr(engine, "projection_status"):
            worker = engine.projection_status() or {}
    except Exception:
        worker = {}
    payload = _diag.collect(
        addon_version=ADDON_VERSION,
        artifact_id=_evolved_artifact_id(),
        anki=versions.get("anki", ""),
        python=platform.python_version(),
        qt=versions.get("qt", ""),
        os_name=platform.system(),
        arch=platform.machine(),
        mode=_evolved_mode_name(),
        error_code=(status.get("recovery_message")
                    or worker.get("failed")
                    or status.get("last_error", "")),
        pending=_evolved_pending(),
        toggles=settings,
        timings={"worker_queue_latency_p95": 0} if worker.get("busy") else {},
        recovery=bool(_RECOVERY_WARNING.get("active")),
        worker_failed=bool(worker.get("failed")),
        logged_in=bool(_evolved_account_info().get("logged_in")))
    try:
        _diag.record(payload)
    except Exception:
        pass
    return payload


def _evolved_recovery_flow():
    from .evolved.ui.dialogs import show_recovery_dialog
    from .evolved import accounts
    from .evolved.net import post_json
    sess, endpoint = _evolved_profile_session(), _evolved_endpoint()
    if sess is None or endpoint is None:
        return {"ok": False, "error": "Account service is not configured."}
    def request(email):
        result = accounts.request_recovery(post_json, endpoint, email=email)
        return {"ok": result.ok, "error": result.error}
    from .evolved.auth import MemorySession
    recovery_session = MemorySession()
    def confirm(fields):
        if len(fields["new_password"]) < 6:
            return {"ok": False, "error": "Use at least 6 characters for your new password."}
        if not recovery_session.logged_in:
            result = accounts.verify_code(post_json, endpoint, email=fields["email"],
                code=fields["code"], kind="recovery", session=recovery_session)
            if not result.ok:
                return {"ok": False, "error": result.error}
        result = accounts.set_new_password(post_json, endpoint,
            access_token=recovery_session.access_token, new_password=fields["new_password"])
        if result.ok:
            sess.session.set(access_token=recovery_session.access_token,
                refresh_token=recovery_session.refresh_token,
                user_id=recovery_session.user_id, username=recovery_session.username)
            _EVOLVED_CTX["user_id"] = sess.user_id
        return {"ok": result.ok, "error": result.error}
    result = show_recovery_dialog(getattr(mw, "ankiscape_evolved_shell", None) or mw, request, confirm)
    _evolved_refresh_views()
    return result


def _evolved_login_submit(fields, sess, endpoint, accounts_mod, post_fn) -> dict:
    """Shared login submit (username-or-email; stub on_submit compatible)."""
    try:
        identity = (fields.get("identity") or "").strip()
        password = fields.get("password") or ""
        sess.remember = bool(fields.get("remember", True) and sess.vault and sess.vault.available)
        if "@" in identity:
            result = accounts_mod.login_password(
                post_fn, endpoint, email=identity, password=password,
                session=sess.session)
        else:
            result = accounts_mod.login_username(
                post_fn, endpoint, username=identity, password=password,
                session=sess.session)
        if result.ok:
            _EVOLVED_CTX["user_id"] = sess.user_id
            try:
                svc = _evolved_sync_service()
                if svc is not None:
                    svc.maybe_sync(on_login=True)
            except Exception:
                pass
            return {"ok": True}
        return {"ok": False, "error": result.error}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)[:200]}


def _evolved_export_backup() -> dict:
    """Export Game Backup → file picker save (Settings button)."""
    try:
        import json as _json
        from aqt.qt import QFileDialog
        from .evolved.backup import export_backup
        engine = _ensure_evolved_engine()
        if engine is None:
            return {"ok": False, "error": "no game"}
        payload = engine.journal.export_game(engine.cfg.game_uuid)
        bundle = export_backup(engine.cfg.game_uuid, payload)
        path, _ = QFileDialog.getSaveFileName(
            getattr(mw, "app", None) and mw or None,
            "Export AnkiScape game backup", f"ankiscape-{engine.cfg.game_uuid[:8]}.json",
            "JSON (*.json)")
        if not path:
            return {"ok": False, "error": "cancelled"}
        with open(path, "w", encoding="utf-8") as fh:
            _json.dump(bundle, fh, indent=2)
        return {"ok": True, "path": path}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)[:200]}


def _evolved_restore_backup() -> dict:
    """Restore Game Backup ← file picker open with hash validation."""
    try:
        import json as _json
        from aqt.qt import QFileDialog
        from aqt.utils import askUser
        from .evolved.backup import validate_backup
        from .evolved.journal import journal_path_for_profile
        from .evolved.journal import Journal as _Journal
        path, _ = QFileDialog.getOpenFileName(
            getattr(mw, "app", None) and mw or None,
            "Restore AnkiScape game backup", "", "JSON (*.json)")
        if not path:
            return {"ok": False, "error": "cancelled"}
        with open(path, encoding="utf-8") as fh:
            data = _json.load(fh)
        bundle = validate_backup(data)
        game_uuid = bundle["game_uuid"]
        if _evolved_reviewer_active():
            return {"ok": False, "error": "Leave the reviewer before restoring a backup."}
        engine = _ensure_evolved_engine()
        different = engine is None or engine.cfg.game_uuid != game_uuid
        message = (f"Restore {len(bundle.get('operations', []))} saved operations "
                   f"from game {game_uuid[:8]}?\n\n")
        message += ("This opens the backup's Evolved game and signs out of the current account. "
                    "Your current game remains saved separately. Anki cards are unchanged."
                    if different else "This merges missing history into the current game. "
                    "Existing operations and Anki cards are preserved.")
        if not askUser(message, parent=getattr(mw, "ankiscape_evolved_shell", None) or mw):
            return {"ok": False, "error": "cancelled"}
        if not different:
            counts = engine.journal.import_game(game_uuid, bundle)
            engine.hydrate()
        else:
            pm = getattr(mw, "pm", None)
            profile_dir = pm.profileFolder() if pm is not None else None
            if not profile_dir:
                return {"ok": False, "error": "no profile"}
            journal = _Journal(journal_path_for_profile(profile_dir, game_uuid))
            try:
                counts = journal.import_game(game_uuid, bundle)
            finally:
                journal.close()
            _evolved_logout()
            activated = int(time.time())
            mw.col.set_config("ankiscape_evolved_player_data", {
                "version": 1, "game_uuid": game_uuid, "activated_at": activated,
                "snapshot_revision": 0,
                "preset": {"skill": "mining", "effective_ts": activated}}, undoable=False)
            if engine is not None:
                engine.journal.close()
            _EVOLVED_CTX.update(engine=None, journal=None, game_uuid=None,
                                sync_service=None, session=None)
            _ensure_evolved_engine()
        _evolved_refresh_views()
        return {"ok": True, **counts}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


def _evolved_diagnostics() -> str:
    try:
        engine = _EVOLVED_CTX.get("engine")
        sess = _EVOLVED_CTX.get("profile_session")
        svc = _EVOLVED_CTX.get("sync_service")
        lines = [f"game={(_EVOLVED_CTX.get('game_uuid') or '?')[:8]}",
                 f"logged_in={bool(sess is not None and sess.logged_in)}",
                 f"pending={_evolved_pending()}"]
        if svc is not None:
            lines.append(f"last_error={svc.job.state.last_error or 'none'}")
            lines.append(f"cursor={svc.job.state.server_cursor or '0'}")
        if engine is not None:
            lines.append(f"processed={len(engine.state.processed_keys)}")
        return "Evolved diagnostics: " + "; ".join(lines)
    except Exception as exc:
        return f"Evolved diagnostics unavailable: {exc!r}"[:300]


def _routing_on_profile_load():
    """Begin the Runtime for this profile and route first-run journeys.

    Fresh installs enter Evolved setup directly; Classic users with real
    progress get an explicit Try Evolved / Continue Classic choice; existing
    Evolved games go straight to Training Home. No restart is ever required.
    """
    if not _RUNTIME_AVAILABLE:
        return
    try:
        rt = _runtime_mod.get_runtime()
        if rt.profile_loaded:
            return  # duplicate hook delivery for the same profile open
    except Exception:
        pass
    try:
        # Resolve the collection FRESH on every use: Anki may close and
        # reopen it during startup (23.10 first-run setup does), which would
        # strand methods bound to the old object (writes silently lost).
        def _col():
            try:
                return getattr(mw, "col", None)
            except Exception:
                return None

        def _get_cfg(key, default=None):
            col = _col()
            if col is None:
                return default
            try:
                return col.get_config(key, default)
            except Exception:
                return default

        def _set_cfg(key, value):
            col = _col()
            if col is None:
                raise RuntimeError("no collection")
            return col.set_config(key, value)

        if _col() is None:
            return
        crumb = {"step": "start"}
        try:
            from .evolved import onboarding as _onb
            raw_request = _get_cfg(_mode_mod.REQUEST_KEY, None)
            requested = _mode_mod.normalize_requested(raw_request)
            draft = _onb.load(_get_cfg)
            pointer = _onb.evolved_identity(_get_cfg)
            classic_progress = _onb.has_meaningful_classic_progress(
                _get_cfg("ankiscape_player_data", {}) or {})
            decision = _onb.decide_first_load(
                requested=raw_request, has_request=raw_request is not None,
                classic_progress=classic_progress, evolved=pointer, draft=draft)
            _EVOLVED_CTX["onboarding"] = draft
            action = decision.get("action")
            crumb.update({"action": action, "reason": decision.get("reason"),
                          "classic": classic_progress, "evolved": bool(pointer),
                          "request": raw_request,
                          "congrats": dict(_CONGRATS_PROBE)})
            _set_cfg("ankiscape_evolved_routing", crumb)

            if action == _onb.SHOW_UPGRADE:
                _begin_runtime_with(_mode_mod.CLASSIC)
                try:
                    from aqt.qt import QTimer  # type: ignore

                    def _ask():
                        try:
                            _ask_upgrade_choice()
                        except Exception:
                            pass

                    QTimer.singleShot(0, _ask)
                    return
                except Exception:
                    return
            if action == _onb.RESUME_ONBOARDING:
                draft.complete = False
                _EVOLVED_CTX["onboarding"] = draft
                _begin_runtime_with(_mode_mod.EVOLVED, run_catchup=False)
                crumb["began"] = "evolved_onboarding"
                _set_cfg("ankiscape_evolved_routing", crumb)
                try:
                    from aqt.qt import QTimer  # type: ignore
                    QTimer.singleShot(0, lambda: _open_evolved_shell())
                except Exception:
                    pass
                return
            if action == _onb.ACTIVATE_EVOLVED:
                # Established Evolved users bypass setup entirely.
                established = _onb.OnboardingState(
                    step="done", gathering_skill="", starting_resource="",
                    complete=True)
                _EVOLVED_CTX["onboarding"] = established
                _begin_runtime_with(_mode_mod.EVOLVED)
                crumb["began"] = "evolved"
                _set_cfg("ankiscape_evolved_routing", crumb)
                return
            _begin_runtime_with(_mode_mod.CLASSIC)
            crumb["began"] = "classic"
            _set_cfg("ankiscape_evolved_routing", crumb)
        except Exception as exc:
            crumb["error"] = repr(exc)[:300]
            try:
                _set_cfg("ankiscape_evolved_routing", crumb)
            except Exception:
                pass
    except Exception:
        pass


def _ask_upgrade_choice() -> str:
    """Upgrade prompt: Try Evolved / Continue Classic. Close keeps Classic.

    Choosing Try Evolved activates setup in the SAME visit (no restart); the
    prompt is asked once because either choice persists the requested mode.
    """
    from .evolved.ui.chooser import qt_upgrade_dialog, show_upgrade_prompt
    col = getattr(mw, "col", None)
    if col is None:
        return _mode_mod.CLASSIC

    def _get(key, default=None):
        try:
            return col.get_config(key, default)
        except Exception:
            return default

    def _set(key, value):
        return col.set_config(key, value)

    try:
        result = show_upgrade_prompt(
            get_requested=lambda: _mode_mod.get_requested_mode(_get),
            set_requested=lambda m: _mode_mod.set_requested_mode(_set, m),
            qt_dialog=qt_upgrade_dialog(getattr(mw, "app", None) and mw))
    except Exception:
        result = _mode_mod.CLASSIC
    if result == _mode_mod.EVOLVED:
        try:
            _switch_mode_now(_mode_mod.EVOLVED, open_onboarding=True)
        except Exception:
            pass
    else:
        try:
            _mode_mod.set_requested_mode(_set, _mode_mod.CLASSIC)
        except Exception:
            pass
    return result


def _begin_runtime_with(requested: str, *, run_catchup: bool = True):
    try:
        rt = _runtime_mod.get_runtime()
        if requested == _mode_mod.EVOLVED:
            from .runtime import EvolvedAdapter
            rt.begin_profile(requested, EvolvedAdapter())
            try:
                from .ui import hide_review_hud as _hide_classic_hud
                _hide_classic_hud()
            except Exception:
                pass
        else:
            from .runtime import ClassicAdapter
            rt.begin_profile(requested, ClassicAdapter(legacy={
                "on_review_answer": lambda ctx: None,
                "on_menu": lambda ctx: _on_main_menu(),
            }))
        _EVOLVED_CTX["generation"] = rt.generation
    except Exception:
        pass
    if not _RUNTIME_AVAILABLE:
        return
    try:
        rt = _runtime_mod.get_runtime()
        if getattr(rt.active_adapter, "name", "") != "evolved":
            return
        if not run_catchup:
            return
        if _evolved_onboarding_active():
            return  # no reward accrual before setup completes
        # Bounded catch-up scan off the review-critical path (fast path:
        # rows newer than the persisted frontier). One summary, no popups.
        try:
            engine = _evolved_engine_if_active()
            from .evolved.catchup import run_catchup
            col = getattr(mw, "col", None)
            if col is not None and engine is not None:
                result = run_catchup(col, engine, engine.journal, full=False)
                if isinstance(result, dict) and result.get("made"):
                    debug_log(f"evolved: catch-up +{result['made']} (profile load)")
        except Exception as exc:
            debug_log(f"evolved: load catch-up failed: {exc!r}")
    except Exception:
        pass


def _evolved_reviewer_active() -> bool:
    """True while a review is open or transitioning (switch must wait)."""
    try:
        if getattr(mw, "state", "") == "review":
            return True
        reviewer = getattr(mw, "reviewer", None)
        rst = str(getattr(reviewer, "state", "") or "")
        if rst in ("question", "answer", "transition"):
            state = str(getattr(mw, "state", "") or "")
            if state not in ("deckBrowser", "overview"):
                return True
    except Exception:
        pass
    return False


def _switch_mode_now(mode: str, *, open_onboarding: bool = False) -> dict:
    """Immediate mode transition (decision 22). No restart, ever.

    Rejected while the reviewer is active. Invalidates the generation before
    releasing widgets/HUD/credentials, then activates the new adapter and
    refreshes entry points. Both stores are preserved.
    """
    if not _RUNTIME_AVAILABLE:
        return {"ok": False, "error": "runtime unavailable"}
    mode = _mode_mod.normalize_requested(mode)
    try:
        if _evolved_reviewer_active():
            return {"ok": False,
                    "error": "Leave the reviewer before switching modes."}
    except Exception:
        pass
    rt = _runtime_mod.get_runtime()
    if not rt.profile_loaded:
        return {"ok": False, "error": "No profile loaded."}
    if getattr(rt.active_adapter, "name", "") == mode:
        if mode == _mode_mod.EVOLVED and open_onboarding:
            _open_evolved_shell()
        return {"ok": True, "already": mode}
    col = getattr(mw, "col", None)

    # 1. Invalidate generation and release the old mode's widgets first.
    try:
        from .evolved.ui.shell import release_shell
        release_shell(mw)
    except Exception:
        pass
    try:
        from .evolved.ui import qt_hud
        qt_hud.release_hud(mw)
    except Exception:
        pass
    try:
        from .ui import hide_review_hud as _hide_classic_hud
        _hide_classic_hud()
    except Exception:
        pass
    try:
        rt.end_profile()
    except Exception:
        pass

    # 2. Persist the selection.
    try:
        if col is not None:
            _mode_mod.set_requested_mode(
                lambda k, v: col.set_config(k, v), mode)
    except Exception:
        pass

    # 3. Activate the new adapter.
    if mode == _mode_mod.EVOLVED:
        from .runtime import EvolvedAdapter
        rt.begin_profile(mode, EvolvedAdapter())
        _EVOLVED_CTX["generation"] = rt.generation
        if col is not None:
            try:
                from .evolved import onboarding as _onb
                state = _onb.load(lambda k, d=None: col.get_config(k, d))
                _EVOLVED_CTX["onboarding"] = state
            except Exception:
                pass
        if not _evolved_onboarding_active():
            try:
                engine = _evolved_engine_if_active()
                from .evolved.catchup import run_catchup
                if col is not None and engine is not None:
                    run_catchup(col, engine, engine.journal, full=False)
            except Exception:
                pass
        try:
            from aqt.qt import QTimer
            QTimer.singleShot(0, lambda: _open_evolved_shell())
        except Exception:
            _open_evolved_shell()
    else:
        from .runtime import ClassicAdapter
        rt.begin_profile(mode, ClassicAdapter(legacy={
            "on_review_answer": lambda ctx: None,
            "on_menu": lambda ctx: _on_main_menu(),
        }))
        _EVOLVED_CTX["generation"] = rt.generation
    try:
        _force_deck_browser_refresh()
    except Exception:
        pass
    try:
        if getattr(mw, "state", "") == "overview":
            mw.moveToState("overview")
    except Exception:
        pass
    debug_log(f"evolved: mode switched to {mode} (generation {rt.generation})")
    return {"ok": True, "mode": mode, "generation": rt.generation}


_UNDO_CAPTURE_INSTALLED = False  # historical: mw.undo wrapping retired 2026-09-09


def _reconcile_undo_state():
    """Reconcile recent Evolved awards against Anki history. Read-only unless
    a retraction/restore operation is genuinely needed (journal SQLite writes
    never touch Anki ops, so this cannot loop back into operation hooks)."""
    try:
        if not _RUNTIME_AVAILABLE:
            return
        rt = _runtime_mod.get_runtime()
        if not rt.profile_loaded:
            return
        if getattr(rt.active_adapter, "name", "") != "evolved":
            return
        engine = _EVOLVED_CTX.get("engine")
        col = getattr(mw, "col", None)
        if engine is None or col is None or getattr(col, "db", None) is None:
            return
        db = col.db

        def _row_exists(revlog_id):
            try:
                return db.scalar("select 1 from revlog where id = ?",
                                 int(revlog_id)) is not None
            except Exception:
                return True  # unknown: never retract on doubt

        result = engine.reconcile_undo(_row_exists)
        if result.get("retracted") or result.get("restored"):
            debug_log(f"evolved: undo reconcile {result}")
    except Exception:
        pass


def _on_operation_did_execute(*_args, **_kwargs):
    """Post-commit hook (23.10+): undo/redo complete asynchronously, so the
    mw.undo() return point is too early to observe the deleted revlog row.
    Reconciling here runs after the transaction commits, on the main thread.
    Recent-window checks are a handful of indexed selects; ordinary ops are
    unaffected no-ops."""
    _reconcile_undo_state()


def _on_collection_sync_finished():
    """Full reconciliation scan after Anki sync (finds late mobile history)."""
    try:
        if not _RUNTIME_AVAILABLE:
            return
        rt = _runtime_mod.get_runtime()
        if not rt.profile_loaded:
            return
        if getattr(rt.active_adapter, "name", "") != "evolved":
            return
        engine = _EVOLVED_CTX.get("engine")
        col = getattr(mw, "col", None)
        if engine is None or col is None:
            return
        from .evolved.catchup import run_catchup
        result = run_catchup(col, engine, engine.journal, full=True)
        if isinstance(result, dict) and result.get("made"):
            debug_log(f"evolved: catch-up +{result['made']} (after sync)")
            try:
                svc = _evolved_sync_service()
                if svc is not None:
                    svc.note_reviews(int(result["made"]))
                    if svc.due(on_anki_sync=True):
                        import threading as _th
                        _th.Thread(target=_evolved_background_sync,
                                   args=(svc,), daemon=True).start()
            except Exception as exc:
                debug_log(f"evolved: post-sync sync failed: {exc!r}")
    except Exception as exc:
        debug_log(f"evolved: sync catch-up failed: {exc!r}")


def _evolved_background_sync(svc) -> None:
    """Run one sync off the main thread; journal SQLite is serialized.

    Contract: no collection/Qt objects cross into workers. The SyncJob only
    touches the journal (thread-safe) and immutable HTTP payloads; the UI
    re-reads status on next menu open / Hiscores refresh.
    """
    try:
        rt = _runtime_mod.get_runtime()
        gen = int(rt.generation or 0)
        user = svc.job.user_id
        svc.job.run_once(generation=gen or svc.job.generation, user_id=user)
    except Exception as exc:
        debug_log(f"evolved: background sync: {exc!r}")


def _routing_on_profile_close():
    """Invalidate generation before releasing widgets (contract A)."""
    try:
        if _RUNTIME_AVAILABLE:
            _runtime_mod.get_runtime().end_profile()
    except Exception:
        pass
    # Stop the projection worker with a bounded join; it owns its connection
    # and finishes/abandons itself, so profile close never blocks on a
    # rebuild. Flush the derived checkpoint first so restarts stay fast.
    try:
        engine = _EVOLVED_CTX.get("engine")
        if engine is not None:
            try:
                engine.flush_checkpoint()
            except Exception:
                pass
            worker = engine.detach_worker()
            if worker is not None:
                try:
                    if not worker.stop(timeout=1.0):
                        debug_log("evolved: projection worker still finishing; "
                                  "its connection closes on thread exit")
                except Exception:
                    pass
    except Exception:
        pass
    _PENDING_REVIEWS.clear()
    _PENDING_POLLS["count"] = 0
    _RECOVERY_WARNING.update({"active": False, "message": "", "since": 0.0})
    # Release Evolved widgets/HUD after generation invalidation.
    try:
        from .evolved.ui.shell import release_shell
        release_shell(mw)
    except Exception:
        pass
    try:
        from .evolved.ui import qt_hud
        qt_hud.release_hud(mw)
    except Exception:
        pass
    try:
        _evolved_end_session()
    except Exception:
        pass
    try:
        sess = _EVOLVED_CTX.get("profile_session")
        if sess is not None:
            try:
                sess.clear()  # credentials cleared even offline
            except Exception:
                pass
    except Exception:
        pass
    try:
        journal = _EVOLVED_CTX.get("journal")
        if journal is not None:
            try:
                journal.close()
            except Exception:
                pass
    except Exception:
        pass
    _EVOLVED_CTX.update({"engine": None, "journal": None, "game_uuid": None,
                         "generation": 0, "user_id": None,
                         "profile_session": None, "sync_service": None,
                         "sync_service_game": None, "sync_service_user": None,
                         "endpoint": None, "endpoint_dev": None,
                         "onboarding": None, "session": None,
                         "hiscores_cache": None, "preselect": None,
                         "self_context": None})
    try:
        from .evolved.ui.hud import HudOwner  # noqa: F401 (owner release point)
    except Exception:
        pass


def _evolved_skill_selection():
    try:
        col = getattr(mw, "col", None)
        if col is not None:
            skill = col.get_config("ankiscape_evolved_current_skill", "mining")
            if isinstance(skill, str) and skill.lower() in (
                    "mining", "woodcutting", "smithing", "crafting", "fishing", "cooking"):
                return skill.lower()
    except Exception:
        pass
    return "mining"


def _evolved_resource_for(skill: str) -> str:
    defaults = {"mining": "Rune essence", "woodcutting": "Tree", "smithing": "Bronze bar",
                "crafting": "Soft clay", "fishing": "Shrimp", "cooking": "Shrimp"}
    try:
        col = getattr(mw, "col", None)
        if col is not None:
            key = f"ankiscape_evolved_current_{skill}"
            val = col.get_config(key, defaults.get(skill, ""))
            if isinstance(val, str) and val:
                return val
    except Exception:
        pass
    return defaults.get(skill, "")


def _ensure_evolved_engine():
    """Lazily bind journal+engine for the active profile (main thread)."""
    from .evolved.data import load_rules
    from .evolved.engine import EngineConfig, EvolvedEngine
    from .evolved.journal import Journal, journal_path_for_profile
    import uuid as _uuid

    col = getattr(mw, "col", None)
    pm = getattr(mw, "pm", None)
    profile_dir = None
    try:
        if pm is not None and hasattr(pm, "profileFolder"):
            profile_dir = pm.profileFolder()
    except Exception:
        profile_dir = None
    game_uuid = None
    activated_at = 0
    try:
        if col is not None:
            pointer = col.get_config("ankiscape_evolved_player_data", None)
            if isinstance(pointer, dict):
                game_uuid = pointer.get("game_uuid") or None
                activated_at = int(pointer.get("activated_at", 0) or 0)
    except Exception:
        pass
    if not game_uuid:
        game_uuid = str(_uuid.uuid4())
        # New identities activate NOW: reviews taken before this moment are
        # never eligible retroactively (setup completion stamps this too).
        activated_at = activated_at or int(time.time())
        try:
            if col is not None:
                col.set_config("ankiscape_evolved_player_data",
                               {"version": 1, "game_uuid": game_uuid,
                                "activated_at": activated_at,
                                "snapshot_revision": 0,
                                "preset": {"skill": "mining", "effective_ts": activated_at}},
                               undoable=False)
        except Exception:
            pass
    if _EVOLVED_CTX.get("engine") is not None and _EVOLVED_CTX.get("game_uuid") == game_uuid:
        return _EVOLVED_CTX["engine"]
    device_id = "desktop"
    try:
        if col is not None:
            stored = col.get_config("ankiscape_evolved_device_id", None)
            if isinstance(stored, str) and stored:
                device_id = stored
            else:
                device_id = str(_uuid.uuid4())
                try:
                    col.set_config("ankiscape_evolved_device_id", device_id, undoable=False)
                except Exception:
                    pass
    except Exception:
        pass
    journal = None
    if profile_dir:
        try:
            journal = Journal(journal_path_for_profile(profile_dir, game_uuid))
        except Exception:
            journal = None
    if journal is None:
        import tempfile
        import os as _os
        fallback = _os.path.join(tempfile.gettempdir(), "ankiscape-evolved-fallback",
                                 game_uuid, "game.sqlite3")
        journal = Journal(fallback)
    engine = EvolvedEngine(EngineConfig(game_uuid=game_uuid, device_id=device_id,
                                        activated_at=activated_at,
                                        rules=load_rules()), journal)
    try:
        engine.hydrate()
    except Exception:
        pass
    try:
        from .evolved.projection_worker import ProjectionWorker
        worker = ProjectionWorker(journal.path, engine.cfg).start()
        worker.notify_dirty()
        engine.attach_worker(worker)
    except Exception as exc:
        debug_log(f"evolved: projection worker unavailable: {exc!r}")
    _EVOLVED_CTX.update({"engine": engine, "journal": journal, "game_uuid": game_uuid,
                         "user_id": _EVOLVED_CTX.get("user_id")})
    return engine


def _review_identity(card) -> tuple:
    """(card_id, revlog_id, revlog_type, review_ts) for an accepted answer."""
    try:
        card_id = int(getattr(card, "id", 0) or 0)
    except (TypeError, ValueError):
        card_id = 0
    revlog_id = 0
    revlog_type = 1
    review_ts = 0
    try:
        col = getattr(mw, "col", None)
        if col is not None and getattr(col, "db", None) is not None:
            row = col.db.first("SELECT id, type FROM revlog WHERE cid = ? ORDER BY id DESC LIMIT 1",
                               card_id)
            # Note: columns are (id, type); timestamp is id (ms).
            if row is not None:
                revlog_id = int(row[0])
                revlog_type = int(row[1])
                review_ts = revlog_id // 1000
    except Exception:
        pass
    if revlog_id <= 0:
        import time as _time
        review_ts = int(_time.time())
        revlog_id = review_ts * 1000
    return card_id, revlog_id, revlog_type, review_ts


def _on_did_answer_card(reviewer, card, ease):
    """Accepted-answer path (after Anki accepts; never pre-scheduling).

    Evolved credits reward policy 2; Classic records a durable no-reward
    skip claim when an Evolved game exists so later catch-up can never
    backfill reviews deliberately taken in Classic. Setup-incomplete
    profiles award nothing and create no game identity.
    """
    if not _RUNTIME_AVAILABLE:
        return
    try:
        rt = _runtime_mod.get_runtime()
        if not rt.profile_loaded:
            return
        role = getattr(rt.active_adapter, "name", "")
        if role not in ("evolved", "classic"):
            return
        try:
            ease_int = int(ease)
        except (TypeError, ValueError):
            return
        card_id, revlog_id, revlog_type, review_ts = _review_identity(card)

        if role == "classic":
            # Review taken while this desktop is in Classic: if an Evolved
            # game is active, durably exclude it from later catch-up.
            if _evolved_identity_active() and not _evolved_onboarding_active():
                try:
                    engine = _evolved_engine_if_active()
                    if engine is not None:
                        engine.skip_direct(revlog_id=revlog_id, card_id=card_id,
                                           ease=ease_int, revlog_type=revlog_type,
                                           review_ts=review_ts,
                                           reason="classic_mode")
                except Exception as exc:
                    debug_log(f"evolved: classic skip failed: {exc!r}")
            return

        if _evolved_onboarding_active():
            # No reward accrual before training setup completes. Reviews made
            # now are never backfilled (activation is stamped at commit).
            return
        try:
            engine = _ensure_evolved_engine()
        except Exception as exc:
            debug_log(f"evolved: journal init failed: {exc!r}")
            return
        skill = _evolved_skill_selection()
        resource = _evolved_resource_for(skill)
        try:
            result = engine.credit_direct(revlog_id=revlog_id, card_id=card_id,
                                          ease=ease_int, revlog_type=revlog_type,
                                          review_ts=review_ts, skill=skill,
                                          resource=resource, reward_policy=2)
        except Exception as exc:
            debug_log(f"evolved: credit failed: {exc!r}")
            return
        if isinstance(result, dict) and result.get("needs_recovery"):
            debug_log("evolved: persistence failed; game requires recovery")
            _evolved_mark_recovery(result.get("error", "persist_failed"))
            # Conservative reconciliation from Anki's accepted revlog happens
            # on the existing undo/sync hooks; never invent the missing
            # training selection or claim the credit was saved.
            try:
                _reconcile_undo_state()
            except Exception:
                pass
            return
        if isinstance(result, dict) and result.get("pending"):
            # Persisted durably; the projection worker publishes the outcome.
            # Finalize exactly once when it lands, and show a quiet updating
            # HUD in the meantime (no reward before completion).
            try:
                _evolved_after_review_event(result, engine, skill, resource)
            except Exception as exc:
                debug_log(f"evolved: pending publish failed: {exc!r}")
            _evolved_finalize_pending()
            if _PENDING_REVIEWS:
                try:
                    from aqt.qt import QTimer as _QTimer
                    _QTimer.singleShot(150, _evolved_finalize_pending)
                    _QTimer.singleShot(200, _evolved_check_completion)
                except Exception:
                    pass
            return
        _evolved_clear_recovery()
        # Publish to the open shell + HUD from persisted reward events (never
        # from answer-button clicks); paused reviews show their reason.
        try:
            _evolved_after_review_event(result, engine, skill, resource)
        except Exception as exc:
            debug_log(f"evolved: post-review publish failed: {exc!r}")
        try:
            from aqt.qt import QTimer as _QTimer
            _QTimer.singleShot(200, _evolved_check_completion)
        except Exception:
            pass
    except Exception:
        pass


def _evolved_record_outcome(engine, key: str, outcome: str, awarded: bool,
                            paused: bool, skill: str = "") -> None:
    """Session recap + sync trigger bookkeeping for one finalized outcome."""
    if not key:
        return
    try:
        from .evolved import session_summary as _ss
        session = _EVOLVED_CTX.get("session")
        if session is not None:
            _ss.record(session, key, outcome, awarded, paused)
    except Exception:
        pass
    if awarded:
        try:
            svc = _EVOLVED_CTX.get("sync_service")
            if svc is not None:
                svc.note_reviews(1)
        except Exception:
            pass


def _evolved_finalize_pending() -> int:
    """Finalize async review outcomes exactly once (session recap, sync
    trigger, reward toast). Undo before completion removes the review from
    Anki history but the pending entry is dropped when its key never appears:
    the retraction suppresses the obsolete award downstream instead."""
    engine = _EVOLVED_CTX.get("engine")
    if engine is None or not _PENDING_REVIEWS:
        return 0
    # Undo-before-completion drops the award downstream; expire captions that
    # can never finalize so the map stays bounded in a long session.
    now = time.time()
    for key in list(_PENDING_REVIEWS.keys()):
        if now - float(_PENDING_REVIEWS[key].get("queued", now)) > 300.0:
            _PENDING_REVIEWS.pop(key, None)
    finalized = 0
    for key in list(_PENDING_REVIEWS.keys()):
        try:
            outcome = engine.outcome_for(key)
        except Exception:
            outcome = None
        if outcome is None:
            continue
        info = _PENDING_REVIEWS.pop(key, {})
        # A confirmed write ends any persistence-recovery state, whether the
        # reward itself was awarded or paused.
        _evolved_clear_recovery()
        _evolved_finalize_one(engine, key, info, outcome)
        finalized += 1
    if finalized:
        try:
            from .evolved.ui.shell import refresh_shell
            if getattr(mw, "ankiscape_evolved_shell", None) is not None:
                refresh_shell(mw)
        except Exception:
            pass
    if _PENDING_REVIEWS:
        status = {}
        try:
            if hasattr(engine, "projection_status"):
                status = engine.projection_status()
        except Exception:
            status = {}
        if status.get("failed"):
            _evolved_mark_recovery(f"projection:{status.get('failed')}")
            _PENDING_REVIEWS.clear()
            return finalized
        _PENDING_POLLS["count"] += 1
        if _PENDING_POLLS["count"] <= 50:
            try:
                from aqt.qt import QTimer as _QTimer
                _QTimer.singleShot(150, _evolved_finalize_pending)
            except Exception:
                pass
    else:
        _PENDING_POLLS["count"] = 0
    return finalized


def _evolved_finalize_one(engine, key: str, info: dict, outcome: dict) -> None:
    awarded = bool(outcome.get("rewarded"))
    outcome_name = str(outcome.get("outcome", "") or "")
    paused = outcome_name in ("paused_materials", "paused_level")
    skill = str(info.get("skill") or outcome.get("skill") or "")
    _evolved_record_outcome(engine, key, outcome_name, awarded, paused, skill)
    try:
        from .evolved.ui import qt_hud
        projection = engine.projection()
        settings = _evolved_get_settings()
        reward = outcome if awarded else None
        pause_text = ""
        if paused and not awarded:
            pause_text = _evolved_pause_text(projection, skill, outcome_name)
        qt_hud.on_review_event(
            mw, projection, skill, reward=reward,
            paused_reason=(pause_text or outcome_name),
            settings={"visible": bool(settings.get("hud_visible", True)),
                      "position": settings.get("hud_position", "bottom"),
                      "ui_scale": settings.get("ui_scale", 100),
                      "celebrations": bool(settings.get("celebrations", True)),
                      "reduced_motion": bool(settings.get("reduced_motion", False)),
                      "sound": bool(settings.get("sound", False))})
        before = int((info.get("before_levels") or {}).get(skill, 1))
        after = int((projection.get("levels") or {}).get(skill, 1))
        if awarded and after > before and bool(settings.get("celebrations", True)):
            qt_hud.celebrate(
                mw, f"Level up: {skill.title()} {after}", kind="level",
                settings={"sound": bool(settings.get("sound", False)),
                          "reduced_motion": bool(settings.get("reduced_motion", False))})
    except Exception as exc:
        debug_log(f"evolved: reward finalize publish failed: {exc!r}")


def _evolved_after_review_event(result, engine, skill: str, resource: str) -> None:
    """HUD/shell/session publication for one persisted review event."""
    result = result or {}
    if result.get("pending"):
        key = result.get("review_key")
        if key:
            _PENDING_REVIEWS[key] = {
                "skill": skill, "resource": resource, "queued": time.time(),
                "before_levels": dict(result.get("before_levels") or {})}
        try:
            _evolved_update_hud_after_reward()
        except Exception:
            pass
        try:
            from .evolved.ui.shell import refresh_shell
            if getattr(mw, "ankiscape_evolved_shell", None) is not None:
                refresh_shell(mw)
        except Exception:
            pass
        return
    outcome = (result or {}).get("outcome", "") or ""
    awarded = bool((result or {}).get("awarded"))
    paused = outcome in ("paused_materials", "paused_level")
    _evolved_record_outcome(engine, (result or {}).get("review_key", ""),
                            outcome, awarded, paused, skill)
    try:
        from .evolved.ui import qt_hud
        projection = engine.projection()
        settings = _evolved_get_settings()
        reward = None
        if awarded:
            reward_outcomes = (projection.get("review_outcomes") or {})
            reward = reward_outcomes.get((result or {}).get("review_key", ""))
        pause_text = ""
        if paused and not awarded:
            pause_text = _evolved_pause_text(projection, skill, outcome)
        qt_hud.on_review_event(
            mw, projection, skill, reward=reward,
            paused_reason=(pause_text or outcome),
            settings={"visible": bool(settings.get("hud_visible", True)),
                      "position": settings.get("hud_position", "bottom"),
                      "ui_scale": settings.get("ui_scale", 100),
                      "celebrations": bool(settings.get("celebrations", True)),
                      "reduced_motion": bool(settings.get("reduced_motion", False)),
                      "sound": bool(settings.get("sound", False))})
        if (result or {}).get("level_up") and bool(settings.get("celebrations", True)):
            qt_hud.celebrate(
                mw, f"Level up: {str(skill).title()} "
                    f"{int((projection.get('levels') or {}).get(skill, 1))}",
                kind="level",
                settings={"sound": bool(settings.get("sound", False)),
                          "reduced_motion": bool(settings.get("reduced_motion", False))})
    except Exception as exc:
        debug_log(f"evolved: hud publish failed: {exc!r}")
    try:
        from .evolved.ui.shell import refresh_shell
        if getattr(mw, "ankiscape_evolved_shell", None) is not None:
            refresh_shell(mw)
    except Exception:
        pass


def _evolved_pause_text(projection, skill: str, outcome: str) -> str:
    try:
        from .evolved.ui.menu_model import training_home
        home = training_home(_evolved_rules(), projection,
                             _evolved_selections(), skill)
        if outcome == "paused_level":
            return (f"Level {home['level_requirement']} "
                    f"{home['skill_label']} required")
        if home.get("missing_text"):
            return f"Need {home['missing_text']}"
        return "Materials needed"
    except Exception:
        return "Paused — no reward"


# Additive hook wiring (import-time dispatch install only; no collection reads).
def _on_congrats_after(*_args, **_kwargs):
    try:
        _evolved_on_completion()
    except Exception:
        pass


_CONGRATS_PROBE: dict = {}


def _install_congrats_wrap() -> None:
    """Completion recap hook: wrap Reviewer._showCongrats once."""
    try:
        try:
            _CONGRATS_PROBE["methods"] = sorted(
                name for name in dir(Reviewer) if "congrat" in name.lower())
        except Exception as exc:
            _CONGRATS_PROBE["methods_error"] = repr(exc)[:200]
        if getattr(Reviewer, "_ankiscape_congrats_wrapped", False):
            return
        original = getattr(Reviewer, "_showCongrats", None)
        _CONGRATS_PROBE["has_show_congrats"] = callable(original)
        if not callable(original):
            return
        Reviewer._showCongrats = wrap(original, _on_congrats_after, "after")  # type: ignore[attr-defined]
        Reviewer._ankiscape_congrats_wrapped = True  # type: ignore[attr-defined]
    except Exception:
        pass


try:
    if _RUNTIME_AVAILABLE:
        try:
            addHook("profileLoaded", _routing_on_profile_load)
        except Exception:
            pass
        try:
            # Anki may close/reopen the collection during first-run setup;
            # route again on each open when no runtime is active.
            gui_hooks.profile_did_open.append(  # type: ignore[attr-defined]
                lambda *a, **k: _routing_on_profile_load())
        except Exception:
            pass
        _install_congrats_wrap()
        try:
            gui_hooks.state_did_change.append(_evolved_on_state_change)  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            gui_hooks.reviewer_will_end.append(_evolved_on_reviewer_will_end)  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            gui_hooks.reviewer_did_answer_card.append(_on_did_answer_card)  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            # Post-commit op hook (23.10+): where Undo/Redo reconciliation
            # lives, since mw.undo() returns before the background op lands.
            gui_hooks.operation_did_execute.append(  # type: ignore[attr-defined]
                _on_operation_did_execute)
        except Exception:
            pass
        try:
            gui_hooks.sync_did_finish.append(  # type: ignore[attr-defined]
                lambda *a, **k: _on_collection_sync_finished())
        except Exception:
            pass
        try:
            gui_hooks.profile_will_close.append(lambda *a, **k: _routing_on_profile_close())  # type: ignore[attr-defined]
        except Exception:
            try:
                gui_hooks.profile_did_close.append(lambda *a, **k: _routing_on_profile_close())  # type: ignore[attr-defined]
            except Exception:
                pass
except Exception:
    pass
