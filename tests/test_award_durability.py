"""Durability and ordering pins for the Classic award path.

Before 3.0.0 the award was credited in memory, then modal level-up and
achievement dialogs ran, and only then was the profile saved. A failure
anywhere in that dialog path therefore discarded a reward the player had
already earned, and the exception escaped into Anki's own ``_answerCard``.

These tests pin the fixed contract:

1. every state mutation completes and is **persisted before any dialog runs**;
2. dialogs are best-effort — a UI failure never propagates into the answer path
   and never loses the reward;
3. the dialog helpers are safe to call when Qt is unavailable.

Qt is deliberately absent in this suite (the fake-Anki bootstrap provides no
``aqt.qt``), so ``ui.HAS_QT`` is False — which is exactly the reachable
production state that used to raise ``NameError`` from the popup path.
"""
import copy
import random
import sys
import unittest

from tests.test_integration_smoke import (
    _install_runtime_fakes,
    _load_addon_as_package,
    _DummyMW,
)

AWARD_ORE = "Rune essence"
START_EXP = 80.0  # +5 exp from Rune essence crosses EXP_TABLE[1] == 83 -> level 2


class _RecordingCol:
    """Config store that snapshots on write and records operation order.

    Deep-copying on ``set_config`` is what makes the snapshot meaningful: the
    live ``player_data`` dict is mutated in place, so if this stored a
    reference, the "persisted" value would appear to update even when no save
    had run. Copying means a test can tell exactly what had been written at the
    moment ``save_player_data()`` executed.
    """

    def __init__(self, store, events):
        self._store = copy.deepcopy(store or {})
        self._events = events

    def get_config(self, key, default=None):
        return copy.deepcopy(self._store.get(key, default))

    def set_config(self, key, value):
        self._events.append(("save", key))
        self._store[key] = copy.deepcopy(value)

    def persisted(self):
        return self._store.get("ankiscape_player_data")


def classic_player_data():
    """A *self-consistent* Classic profile: level 1 with 80 exp, one award from
    level 2. Deliberately not dev/fixtures.classic_player_data(), whose
    level 23 / 50000 exp pair is internally inconsistent (it implies level 42)."""
    ores = {ore: 0 for ore in ("Rune essence", "Clay", "Copper ore", "Tin ore",
                               "Iron ore", "Coal", "Silver ore", "Gold ore",
                               "Mithril ore", "Adamantite ore", "Runite ore")}
    ores["Rune essence"] = 320
    return {"config_version": 2, "mining_level": 1, "woodcutting_level": 1,
            "smithing_level": 1, "crafting_level": 1, "mining_exp": START_EXP,
            "woodcutting_exp": 0, "smithing_exp": 0, "crafting_exp": 0,
            "current_craft": "", "current_ore": AWARD_ORE, "current_tree": "Oak",
            "current_bar": "Steel bar", "inventory": ores, "progress_to_next": 0,
            "completed_achievements": []}


class AwardDurabilityTest(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)
        self._orig_random = random.random
        _install_runtime_fakes()

    def tearDown(self):
        random.random = self._orig_random  # never leak a forced RNG
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def _drive(self, *, level_up_raises=False, achievement_raises=False,
               force_achievement=False, skip_achievement_patch=False):
        """Answer one card through the real hook order and return the fixture."""
        addon = _load_addon_as_package(mod_name="ankiscape_award_durability")

        events = []
        col = _RecordingCol({"ankiscape_current_skill": "Mining",
                             "ankiscape_popups_enabled": True}, events)
        addon.mw = _DummyMW(col)

        # Neutralise Qt-only presentation surfaces that are not under test.
        addon.ensure_review_hud = lambda *a, **k: None
        addon.update_review_hud = lambda *a, **k: None
        addon.hide_review_hud = lambda *a, **k: None
        addon._show_exp = lambda *a, **k: None
        addon._refresh_skill_availability = lambda *a, **k: None
        try:
            addon.ui.mw = addon.mw
        except Exception:
            pass

        addon.player_data = classic_player_data()
        addon.current_skill = "Mining"

        # storage.py and ui.py each did `from aqt import mw` at import time, so
        # binding addon.mw alone is not enough — every module-level binding of
        # the Anki main window has to point at the recording store.
        for holder in (sys.modules.get("aqt"), addon, addon.storage, addon.ui):
            if holder is not None:
                try:
                    holder.mw = addon.mw
                except Exception:
                    pass

        # Record and optionally fail the two modal popups. Patched on the logic
        # module, which is where show_level_up_popups / show_achievement_popups
        # look the names up.
        logic = addon.logic

        def level_up_dialog(skill):
            events.append(("popup", f"level_up:{skill}"))
            if level_up_raises:
                raise RuntimeError("injected level-up dialog failure")

        def achievement_dialog(name, data):
            events.append(("popup", f"achievement:{name}"))
            if achievement_raises:
                raise RuntimeError("injected achievement dialog failure")

        logic.show_level_up_dialog = level_up_dialog
        logic.show_achievement_dialog = achievement_dialog
        if force_achievement:
            logic.get_newly_completed_achievements = (
                lambda player_data, achievements: ["First Steps"])

        random.random = lambda: 0.0  # every probability roll succeeds

        answered = []
        old_answer_card = lambda self, ease: answered.append(ease)

        # The order Anki fires these in: question hook, answer hook (x2), then
        # the wrapped Reviewer._answerCard.
        addon.on_card_did_show(card=object())
        addon.on_card_did_show(card=object())
        addon.on_show_answer(reviewer=object())
        returned = addon.on_answer_card(_FakeReviewerStub(), 3, _old=old_answer_card)

        return addon, col, events, answered, returned

    def test_award_is_persisted_before_any_dialog_runs(self):
        addon, col, events, answered, _ = self._drive()

        self.assertGreater(addon.player_data["mining_exp"], START_EXP,
                           "the forced-success roll must credit exp")
        self.assertEqual(addon.player_data["mining_level"], 2,
                         "80 exp + 5 must cross EXP_TABLE[1] == 83 into level 2")
        self.assertEqual(answered, [3], "the wrapped answer must still submit")

        saves = [i for i, (kind, _) in enumerate(events) if kind == "save"]
        popups = [i for i, (kind, _) in enumerate(events) if kind == "popup"]
        self.assertTrue(popups, "a level-up popup was expected for this award")
        self.assertTrue(saves, "the award must be persisted")
        self.assertLess(min(saves), min(popups),
                        f"the profile must be saved before any dialog runs "
                        f"(events were {events})")

        persisted = col.persisted()
        self.assertIsNotNone(persisted, "no player data reached the config store")
        self.assertEqual(persisted["mining_exp"], addon.player_data["mining_exp"],
                         "the persisted exp must match the credited exp")
        self.assertEqual(persisted["mining_level"], 2,
                         "the level-up must be persisted, not just held in memory")

    def test_level_up_dialog_failure_loses_nothing_and_aborts_nothing(self):
        addon, col, events, answered, _ = self._drive(level_up_raises=True)

        self.assertEqual(answered, [3],
                         "a dialog failure must not stop the answer being submitted")
        persisted = col.persisted()
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["mining_exp"], addon.player_data["mining_exp"],
                         "the credited reward must survive a dialog failure")
        self.assertGreater(persisted["mining_exp"], START_EXP)
        self.assertEqual(persisted["mining_level"], 2)

    def test_achievement_dialog_failure_loses_nothing_and_aborts_nothing(self):
        addon, col, events, answered, _ = self._drive(
            achievement_raises=True, force_achievement=True)

        self.assertEqual(answered, [3])
        self.assertIn(("popup", "achievement:First Steps"), events,
                      "the achievement popup path must have been exercised")
        persisted = col.persisted()
        self.assertEqual(persisted["mining_exp"], addon.player_data["mining_exp"])
        self.assertEqual(persisted["mining_level"], 2)

    def test_dialog_helpers_are_safe_without_qt(self):
        """ui.HAS_QT is False here; none of these may raise."""
        addon = _load_addon_as_package(mod_name="ankiscape_award_durability_qt")
        addon.mw = _DummyMW(_RecordingCol({}, []))

        addon.ui.show_level_up_dialog("Mining")
        addon.ui.show_achievement_dialog("First Steps", {"description": "d"})
        addon.ui.show_error_message("title", "message")


class _FakeReviewerStub:
    def _answerCard(self, ease):
        return None


if __name__ == "__main__":
    unittest.main()
