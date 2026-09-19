# tests/test_classic_awards.py - Classic-mode XP award gate regression pin.
"""Pins the Classic award gate: hook registration order, the wrapped answer
hook, and the per-card lifecycle that decides whether XP is credited.

Why this file exists
--------------------
`on_answer_card` credits XP only when `on_show_answer` has already run for the
card being answered, and that depends entirely on the order the add-on registers
its callbacks on `reviewer_did_show_answer`. The existing
tests/test_hooks_registration.py pins *counts* of a synthetic plan, never the
real registered order - so when a release audit found Classic crediting no XP
after an upgrade, a flaky *harness* assertion could not be told apart from a
broken *product*: nothing in the suite exercised the gate at all.

These tests drive the real registered hook lists and the real wrapped
`Reviewer._answerCard` for the real `upgrade` fixture, so a future change to the
registration order or to the per-card lifecycle fails here instead of in the
field.

Conventions (runtime fakes, package loading) follow tests/test_integration_smoke.py;
expected values are always read from product data (constants.ORE_DATA via the
loaded package) or computed with public functions, never copied from the
implementation.
"""
from __future__ import annotations

import importlib.util
import inspect
import random
import sys
import types
import unittest
from contextlib import contextmanager
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SKILL = "Mining"
EASE_GOOD = 3  # Anki's "Good" button
MISS_DRAW = 1.0  # >= every success probability the game can produce -> always a miss
GEM_DROP_CHANCE = 1 / 256  # Classic mining gem roll, see _success_draw()
_DELEGATED = "delegated-to-old"


def _load_module(name: str, relative: str):
    """Load a repo module by path (convention: tests/test_fixture_plans.py)."""
    spec = importlib.util.spec_from_file_location(name, str(ROOT / relative))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The repo's own Classic scenario fixture: the same player state the `upgrade`
# journey seeds, so this test cannot drift from the shipped scenario.
_FIXTURES = _load_module("ankiscape_classic_awards_fixtures", "dev/fixtures.py")


# --- Minimal fakes for the Anki runtime (same bootstrap as the smoke test) ---
class _FakeHooks:
    def __init__(self):
        # Lists emulate Anki's gui_hooks collections with append/remove
        self.overview_did_refresh = []
        self.overview_will_refresh = []
        self.reviewer_did_show_question = []
        self.reviewer_did_show_answer = []
        self.webview_did_receive_js_message = []


class _FakeReviewer:
    def _answerCard(self, ease):
        return None


# The add-on replaces Reviewer._answerCard at import time. Keep the pristine
# baseline so each test can restore it and observe the wrap exactly once.
_ORIGINAL_ANSWER_CARD = _FakeReviewer._answerCard


class _DummyCol:
    def __init__(self, store=None):
        self._store = dict(store or {})

    def get_config(self, key, default=None):
        return self._store.get(key, default)

    def set_config(self, key, value):
        self._store[key] = value


class _DummyMW:
    def __init__(self, col):
        self.col = col


class _DummyCard:
    id = 1


def _install_runtime_fakes():
    # Undo any wrap a previous test installed, or the second install would wrap
    # the wrapper and every callback would fire twice.
    _FakeReviewer._answerCard = _ORIGINAL_ANSWER_CARD

    # aqt base module
    aqt = types.ModuleType("aqt")
    aqt.mw = None
    aqt.gui_hooks = _FakeHooks()

    # aqt.reviewer submodule
    aqt_reviewer = types.ModuleType("aqt.reviewer")
    aqt_reviewer.Reviewer = _FakeReviewer

    # DO NOT provide aqt.qt so ui.py falls back to HAS_QT = False
    sys.modules["aqt"] = aqt
    sys.modules["aqt.reviewer"] = aqt_reviewer

    # anki.hooks module
    anki_hooks = types.ModuleType("anki.hooks")

    def addHook(_name, _fn):
        return None

    def wrap(old, new, _mode):
        # Mirrors anki.hooks.wrap(pos="around") for the answered-card hook.
        def _wrapped(self, ease, *args, **kwargs):
            return new(self, ease, _old=old)

        return _wrapped

    anki_hooks.addHook = addHook
    anki_hooks.wrap = wrap
    sys.modules["anki.hooks"] = anki_hooks


def _load_addon_as_package(mod_name: str):
    # Load top-level __init__.py as a package so relative imports resolve.
    root = ROOT
    init_py = root / "__init__.py"
    loader = SourceFileLoader(mod_name, str(init_py))
    spec = spec_from_loader(mod_name, loader, is_package=True)
    mod = module_from_spec(spec)
    # Package modules need a __path__ so relative imports work
    mod.__path__ = [str(root)]  # type: ignore[attr-defined]
    sys.modules[mod_name] = mod
    loader.exec_module(mod)
    return mod


@contextmanager
def _forced_random(value):
    """Replace random.random() with a single deterministic draw.

    The Classic award is a `draw < probability` comparison, so returning one
    constant for *every* draw makes the outcome independent of how many draws the
    award path makes and in what order - unlike a queue of draws, which would
    silently reorder if the implementation ever added one. The patch is always
    removed on exit; tearDown() additionally fails the test if it ever is not.
    """
    original = random.random
    random.random = lambda: value  # type: ignore[assignment]
    try:
        yield value
    finally:
        random.random = original


class ClassicAwardGateTests(unittest.TestCase):
    """Drives the Classic award path through the real registered hooks."""

    # --- harness -------------------------------------------------------

    def setUp(self):
        # Isolate sys.modules pollution between tests (smoke-test convention).
        self._modules_before = dict(sys.modules)
        # Seal the global RNG: keep both the entry point and the generator state
        # so a leaked patch can never flatten a later test into a false pass.
        self._random_random = random.random
        self._random_state = random.getstate()
        _install_runtime_fakes()

    def tearDown(self):
        # Report a leaked patch *and* repair it: a patch that outlives the test
        # makes every probabilistic test after it pass for the wrong reason.
        leaked = random.random is not self._random_random
        random.random = self._random_random
        random.setstate(self._random_state)
        sys.modules.clear()
        sys.modules.update(self._modules_before)
        if leaked:
            self.fail("random.random was left patched by this test")

    def _load_addon(self):
        """Load a pristine add-on package and wire up a dummy main window."""
        # Stand in for the reviewer's own _answerCard so the wrapped entry point
        # can be observed delegating to it. The add-on captures this function
        # when it installs its wrapper, i.e. exactly where Anki's real one goes.
        calls = []

        def _recording_answer_card(self, ease):
            calls.append(ease)
            return _DELEGATED

        _FakeReviewer._answerCard = _recording_answer_card
        self.baseline_answer_card = _recording_answer_card
        addon = _load_addon_as_package(
            f"ankiscape_classic_awards_{self._testMethodName}")
        self.old_calls = calls
        self.gui_hooks = sys.modules["aqt"].gui_hooks

        # A product setting, not a stub: level-up / achievement popups need Qt,
        # and turning them off leaves the real level and achievement logic in
        # the path while keeping the dialogs out of it.
        self.col = _DummyCol({
            "ankiscape_current_skill": SKILL,
            "ankiscape_popups_enabled": False,
        })
        self.mw = _DummyMW(self.col)
        addon.mw = self.mw
        # logic.py re-imports mw lazily (`from aqt import mw` inside the call),
        # so the fake module needs it too.
        sys.modules["aqt"].mw = self.mw
        for submodule in ("ui", "storage"):
            try:
                getattr(addon, submodule).mw = self.mw  # type: ignore[attr-defined]
            except Exception:
                pass

        addon.player_data = _FIXTURES.classic_player_data()
        addon.current_skill = SKILL
        return addon

    # --- driving the real hooks ----------------------------------------

    @staticmethod
    def _invoke(callback, payload):
        """Call a registered hook with only the payload keys it declares.

        Anki's hook payloads differ per hook (`card` for the question/answer
        hooks), and the Classic HUD callbacks take no arguments at all.
        """
        try:
            params = inspect.signature(callback).parameters
        except (TypeError, ValueError):
            return callback()
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return callback(**payload)
        return callback(**{k: v for k, v in payload.items() if k in params})

    def _fire(self, hook_list, skip=()):
        """Invoke every registered callback of one hook, in registration order."""
        # Anki hands the card to both reviewer_did_show_question and
        # reviewer_did_show_answer; the Classic answer callback names that
        # parameter `reviewer`.
        payload = {"card": _DummyCard(), "reviewer": _DummyCard()}
        for callback in list(hook_list):
            if callback in skip:
                continue
            self._invoke(callback, payload)

    def _show_card(self, skip_show_answer=False):
        """Fire one full question -> answer -> answered-card sequence."""
        self._fire(self.gui_hooks.reviewer_did_show_question)
        self._fire(self.gui_hooks.reviewer_did_show_answer,
                   skip=(self.addon.on_show_answer,) if skip_show_answer else ())

    def _answer_card(self, ease=EASE_GOOD):
        """Answer through the *real* wrapped Reviewer._answerCard."""
        reviewer = sys.modules["aqt.reviewer"].Reviewer()
        return reviewer._answerCard(ease)

    # --- expectations derived from product data -------------------------

    def _expected_award(self):
        """The XP the award must credit, read from the ore balance table."""
        ore = self.addon.player_data["current_ore"]
        return self.addon.ORE_DATA[ore]["exp"]

    def _success_draw(self):
        """A forced draw that both succeeds and cannot drop a gem.

        Half the real success probability, computed with the product's own
        probability function: strictly below it (so the action succeeds) and far
        above the gem drop chance (so the credited amount is exactly the ore's
        exp, with no bonus gem exp mixed in).
        """
        data = self.addon.player_data
        ore = data["current_ore"]
        chance = self.addon.calculate_mining_probability(
            data["mining_level"], self.addon.ORE_DATA[ore]["probability"])
        draw = chance / 2.0
        self.assertGreater(draw, 0.0, "a zero draw is not a valid success")
        self.assertGreater(chance, draw, "the forced draw must succeed")
        self.assertGreaterEqual(
            draw, GEM_DROP_CHANCE,
            "the forced draw must not also trigger the gem roll, or the "
            "credited amount would exceed ORE_DATA[...]['exp']")
        return draw

    # --- assertion 3: the registration order is pinned ------------------

    def test_answer_hook_registration_order_is_pinned(self):
        self.addon = self._load_addon()

        registered = self.gui_hooks.reviewer_did_show_answer
        self.assertEqual(
            [getattr(cb, "__name__", repr(cb)) for cb in registered],
            ["on_card_did_show", "on_show_answer", "_on_rev_show_answer"],
            "the Classic award gate depends on this exact order: on_card_did_show "
            "must reset the per-card flags before on_show_answer sets them")
        self.assertIs(registered[0], self.addon.on_card_did_show)
        self.assertIs(registered[1], self.addon.on_show_answer)
        self.assertIs(registered[2], self.addon._on_rev_show_answer)

        # The gate also needs on_card_did_show on the question hook to arm the
        # card. Only its presence is pinned there: unlike the answer hook that
        # list holds nothing order-dependent (`_on_rev_show_question` is HUD
        # chrome), and the answer-side copy re-arms the card anyway.
        self.assertIn(self.addon.on_card_did_show,
                      self.gui_hooks.reviewer_did_show_question)

    # --- assertion 1: the award happens ---------------------------------

    def test_full_sequence_awards_the_ore_exp_through_the_wrapped_answer_card(self):
        self.addon = self._load_addon()
        self.assertIsNot(
            _FakeReviewer._answerCard, self.baseline_answer_card,
            "loading the add-on must install its wrapper on Reviewer._answerCard")

        expected = self._expected_award()
        before = self.addon.player_data["mining_exp"]

        with _forced_random(self._success_draw()):
            self._show_card()
            returned = self._answer_card(EASE_GOOD)

        self.assertEqual(returned, _DELEGATED,
                         "the wrapper must still hand the answer to _old")
        self.assertEqual(self.old_calls, [EASE_GOOD])
        after = self.addon.player_data["mining_exp"]
        self.assertGreater(after, before, "answering correctly must credit XP")
        self.assertEqual(after - before, expected,
                         "the award must credit exactly the ore's exp")
        self.assertEqual(
            self.col.get_config("ankiscape_player_data")["mining_exp"], after,
            "the award must be persisted, not just held in memory")

    # --- assertion 2: the guarded negative ------------------------------

    def test_award_does_not_fire_without_the_show_answer_hook(self):
        self.addon = self._load_addon()
        # The skip is only meaningful if the hook really is registered: without
        # this, an unregistered on_show_answer would make the test vacuous.
        self.assertIn(self.addon.on_show_answer,
                      self.gui_hooks.reviewer_did_show_answer)

        before = self.addon.player_data["mining_exp"]
        with _forced_random(self._success_draw()):
            self._show_card(skip_show_answer=True)
            returned = self._answer_card(EASE_GOOD)

        # The wrapper ran and delegated - it simply declined to credit, because
        # the answer was never shown to the player.
        self.assertEqual(returned, _DELEGATED)
        self.assertEqual(self.old_calls, [EASE_GOOD])
        self.assertEqual(self.addon.player_data["mining_exp"], before,
                         "XP must not be credited when on_show_answer never ran")

    # --- assertion 4: a genuine miss is not a lockout --------------------

    def test_a_missed_roll_does_not_lock_out_the_next_card(self):
        self.addon = self._load_addon()
        start = self.addon.player_data["mining_exp"]

        # Card 1: the roll misses, so nothing is credited.
        with _forced_random(MISS_DRAW):
            self._show_card()
            self._answer_card(EASE_GOOD)
        self.assertEqual(self.addon.player_data["mining_exp"], start,
                         "a missed roll must credit nothing")

        # Card 2: the very next card must be able to award again.
        expected = self._expected_award()
        with _forced_random(self._success_draw()):
            self._show_card()
            self._answer_card(EASE_GOOD)
        self.assertEqual(self.old_calls, [EASE_GOOD, EASE_GOOD],
                         "both answers must go through the wrapped method")
        self.assertEqual(self.addon.player_data["mining_exp"], start + expected,
                         "a miss must not lock the player out of later awards")

    # --- the 3.0 routing guard, driven the same way ----------------------

    def test_classic_award_stays_closed_while_the_evolved_adapter_is_active(self):
        self.addon = self._load_addon()
        runtime = self.addon.runtime
        rt = runtime.get_runtime()
        rt.profile_loaded = True
        rt.active_adapter = runtime.EvolvedAdapter(game_uuid="evolved-test")

        before = self.addon.player_data["mining_exp"]
        with _forced_random(self._success_draw()):
            self._show_card()
            returned = self._answer_card(EASE_GOOD)

        self.assertEqual(returned, _DELEGATED,
                         "Evolved must pass the answer through unchanged")
        self.assertEqual(self.addon.player_data["mining_exp"], before,
                         "the Classic award must not fire in Evolved mode")


if __name__ == "__main__":
    unittest.main()
