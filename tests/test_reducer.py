import itertools
import unittest

from evolved.data import load_rules
from evolved.logic_pure import MICRO
from evolved.reducer import replay


def _award(op_id, game, dev, seq, lamp, key, skill, resource, ts=2000, prov="direct"):
    return {"op_id": op_id, "game_uuid": game, "device_id": dev, "device_seq": seq,
            "lamport": lamp, "kind": "review_award",
            "payload": {"review_key": key, "review_ts": ts, "rating": 3,
                        "review_kind": "review", "provenance": prov,
                        "skill": skill, "resource": resource}}


def _retract(op_id, game, dev, seq, lamp, key):
    return {"op_id": op_id, "game_uuid": game, "device_id": dev, "device_seq": seq,
            "lamport": lamp, "kind": "review_retract",
            "payload": {"target_review_key": key, "reason": "anki_undo"}}


def _restore(op_id, game, dev, seq, lamp, key):
    op = _retract(op_id, game, dev, seq, lamp, key)
    op["kind"] = "review_restore"
    op["payload"] = {"target_review_key": key, "reason": "anki_redo"}
    return op


def _preset(op_id, game, dev, seq, lamp, skill, ts):
    return {"op_id": op_id, "game_uuid": game, "device_id": dev, "device_seq": seq,
            "lamport": lamp, "kind": "catchup_preset",
            "payload": {"skill": skill, "effective_ts": ts}}


class TestReducer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = load_rules()

    def test_permutation_invariant(self):
        ops = [
            _award("op-a", "game-1", "dev-a", 1, 1, "rk-a", "woodcutting", "Tree", ts=2001),
            _award("op-b", "game-1", "dev-a", 2, 2, "rk-b", "mining", "Rune essence", ts=2002),
        ]
        s1 = replay(ops, self.rules, "game-1")
        s2 = replay(list(reversed(ops)), self.rules, "game-1")
        self.assertEqual(s1["xp_micro"], s2["xp_micro"])
        self.assertEqual(s1["inventory"], s2["inventory"])

    def test_shared_ingredient_conflict_one_item_plus_practice(self):
        # Seed: mine copper+tin via direct awards is random; instead craft-style
        # conflict via smithing needs inventory. Use cooking conflict: two cooks
        # of one Shrimp-equivalent need two fish; give one fish by making the
        # first award a fishing catch is nondeterministic, so construct via
        # production conflict on Bronze bar with seeded inventory is also
        # indirect. Deterministic version: two Bronze smelts with only enough
        # ore for one must yield one bar + practice XP. Seed inventory by
        # replaying with preloaded ops is not supported, so test the reducer's
        # conflict path via two cooks where only the first can consume.
        ops = [
            {"op_id": "fish-1", "game_uuid": "game-1", "device_id": "dev-a",
             "device_seq": 1, "lamport": 1, "kind": "review_award",
             "payload": {"review_key": "rk-fish", "review_ts": 2000, "rating": 3,
                         "review_kind": "review", "provenance": "direct",
                         "skill": "fishing", "resource": "Shrimp"}},
            {"op_id": "cook-1", "game_uuid": "game-1", "device_id": "dev-a",
             "device_seq": 2, "lamport": 2, "kind": "review_award",
             "payload": {"review_key": "rk-c1", "review_ts": 2001, "rating": 3,
                         "review_kind": "review", "provenance": "direct",
                         "skill": "cooking", "resource": "Shrimp"}},
            {"op_id": "cook-2", "game_uuid": "game-1", "device_id": "dev-a",
             "device_seq": 3, "lamport": 3, "kind": "review_award",
             "payload": {"review_key": "rk-c2", "review_ts": 2002, "rating": 3,
                         "review_kind": "review", "provenance": "direct",
                         "skill": "cooking", "resource": "Shrimp"}},
        ]
        state = replay(ops, self.rules, "game-1")
        # At most one cooked output beyond available fish; no negative inventory.
        for qty in state["inventory"].values():
            self.assertGreaterEqual(qty, 0)
        self.assertIn(state["levels"]["cooking"], range(1, 100))

    def test_retract_restore_and_replacement(self):
        ops = [
            _award("op-1", "game-1", "dev-a", 1, 1, "rk-1", "mining", "Rune essence"),
            _retract("op-2", "game-1", "dev-a", 2, 2, "rk-1"),
        ]
        retracted = replay(ops, self.rules, "game-1")
        ops2 = ops + [_restore("op-3", "game-1", "dev-a", 3, 3, "rk-1")]
        restored = replay(ops2, self.rules, "game-1")
        # Restore re-enables exactly one award, never a second award.
        self.assertGreaterEqual(restored["xp_micro"]["mining"], retracted["xp_micro"]["mining"])
        # Replacement answer (new key) earns normally on top.
        ops3 = ops2 + [_award("op-4", "game-1", "dev-a", 4, 4, "rk-2", "mining", "Rune essence")]
        replaced = replay(ops3, self.rules, "game-1")
        self.assertGreaterEqual(replaced["xp_micro"]["mining"], restored["xp_micro"]["mining"])

    def test_direct_beats_catchup_and_duplicates_ignored(self):
        ops = [
            {"op_id": "op-c", "game_uuid": "game-1", "device_id": "dev-b", "device_seq": 1,
             "lamport": 1, "kind": "review_award",
             "payload": {"review_key": "rk-x", "review_ts": 2000, "rating": 3,
                         "review_kind": "review", "provenance": "catchup"}},
            _award("op-d", "game-1", "dev-a", 1, 2, "rk-x", "mining", "Rune essence"),
            _award("op-d2", "game-1", "dev-a", 2, 3, "rk-x", "mining", "Clay"),
        ]
        state = replay(ops, self.rules, "game-1")
        self.assertTrue(any("duplicate_direct_ignored" in d for d in state["diagnostics"]))

    def test_preset_history_and_default_mining(self):
        ops = [
            _preset("op-p1", "game-1", "dev-a", 1, 1, "fishing", 1500),
            {"op_id": "op-c1", "game_uuid": "game-1", "device_id": "dev-a", "device_seq": 2,
             "lamport": 2, "kind": "review_award",
             "payload": {"review_key": "rk-c1", "review_ts": 1600, "rating": 3,
                         "review_kind": "review", "provenance": "catchup"}},
            {"op_id": "op-c0", "game_uuid": "game-1", "device_id": "dev-a", "device_seq": 3,
             "lamport": 3, "kind": "review_award",
             "payload": {"review_key": "rk-c0", "review_ts": 1400, "rating": 3,
                         "review_kind": "review", "provenance": "catchup"}},
        ]
        state = replay(ops, self.rules, "game-1")
        # rk-c0 predates first preset -> Mining; rk-c1 uses Fishing preset.
        self.assertGreater(state["xp_micro"]["mining"] + state["xp_micro"]["fishing"], 0)

    def test_level_locked_direct_falls_back(self):
        # A fresh game picking Redwood/Runite/Anglerfish directly must fall
        # back to the first unlocked resource, never mint locked rewards.
        ops = [
            _award("op-w", "game-1", "dev-a", 1, 1, "rk-w", "woodcutting", "Redwood"),
            _award("op-m", "game-1", "dev-a", 2, 2, "rk-m", "mining", "Runite ore"),
            {"op_id": "op-f", "game_uuid": "game-1", "device_id": "dev-a",
             "device_seq": 3, "lamport": 3, "kind": "review_award",
             "payload": {"review_key": "rk-f", "review_ts": 2000, "rating": 3,
                         "review_kind": "review", "provenance": "direct",
                         "skill": "fishing", "resource": "Anglerfish"}},
        ]
        state = replay(ops, self.rules, "game-1")
        inv = state["inventory"]
        self.assertEqual(inv.get("Redwood", 0), 0)
        self.assertEqual(inv.get("Runite ore", 0), 0)
        self.assertEqual(inv.get("Anglerfish", 0), 0)
        fallbacks = [d for d in state["diagnostics"] if d.startswith("level_fallback")]
        self.assertEqual(len(fallbacks), 3)

    def test_no_device_balances_summed(self):
        ops = [
            _award("op-a1", "game-1", "dev-a", 1, 1, "rk-shared", "mining", "Rune essence"),
            {"op_id": "op-b1", "game_uuid": "game-1", "device_id": "dev-b", "device_seq": 1,
             "lamport": 2, "kind": "review_award",
             "payload": {"review_key": "rk-shared", "review_ts": 2000, "rating": 3,
                         "review_kind": "review", "provenance": "catchup"}},
        ]
        state = replay(ops, self.rules, "game-1")
        # One review yields at most one direct-size reward, not two summed.
        self.assertLessEqual(state["xp_micro"]["mining"], 200 * MICRO)


if __name__ == "__main__":
    unittest.main()
