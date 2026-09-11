# tests/test_projection_incremental.py - Fast path must equal full replay.
"""Property traces compare the incremental checkpoint extension with the
reference reducer at every prefix, including policies 1/2, mined gems, the
cooking gate, dependent recipes, multiple devices, skips, catch-up presets
and retract/restore. Invalidating cases must fall back to a rebuild."""
from __future__ import annotations

import random
import unittest

from evolved.data import load_rules
from evolved.reducer import (
    CHECKPOINT_VERSION, REDUCER_VERSION, build_checkpoint, canonical_order_key,
    extend_checkpoint, replay, rules_hash,
)

GAME = "game-inc-1"
# Deterministic gem drop key (from test_reducer): Clay action roll hits, the
# 1/256 gem roll hits, the first bracket grants an uncut sapphire.
GEM_KEY = ("6dc859e8a122c510eb5d132330389487"
           "416fe601d89efb802cf118e85193c8c9")


def _award(i, device="dev-a", seq=None, lamport=None, key=None, skill="mining",
           resource="Rune essence", policy=2, prov="direct", ts=None, game=GAME):
    return {"op_id": f"op-{device}-{i}", "game_uuid": game, "device_id": device,
            "device_seq": seq if seq is not None else i,
            "lamport": lamport if lamport is not None else i,
            "kind": "review_award",
            "payload": {"review_key": key or f"rk-{device}-{i}",
                        "review_ts": ts if ts is not None else 2000 + i,
                        "rating": 3, "review_kind": "review",
                        "provenance": prov, "reward_policy": policy,
                        "skill": skill, "resource": resource}}


def _skip(i, device="dev-a", key="rk-skip", seq=None, lamport=None):
    return {"op_id": f"skip-{device}-{i}", "game_uuid": GAME, "device_id": device,
            "device_seq": seq if seq is not None else i,
            "lamport": lamport if lamport is not None else i,
            "kind": "review_skip",
            "payload": {"review_key": key, "reason": "classic_mode",
                        "provenance": "skip"}}


def _retract(i, key, kind="review_retract", device="dev-a", seq=None, lamport=None):
    return {"op_id": f"{kind}-{device}-{i}", "game_uuid": GAME,
            "device_id": device, "device_seq": seq if seq is not None else i,
            "lamport": lamport if lamport is not None else i, "kind": kind,
            "payload": {"target_review_key": key, "reason": "test"}}


def _preset(i, skill, ts, device="dev-a"):
    return {"op_id": f"preset-{device}-{i}", "game_uuid": GAME,
            "device_id": device, "device_seq": i, "lamport": i,
            "kind": "catchup_preset",
            "payload": {"skill": skill, "effective_ts": ts}}


def _identical(left, right, keys=None):
    keys = keys or ("xp_micro", "levels", "inventory", "achievements",
                    "revision", "total_level", "review_outcomes", "counters",
                    "skipped", "diagnostics", "adjustments", "conflicts",
                    "per_skill_success")
    for key in keys:
        if left.get(key) != right.get(key):
            return key
    return None


class IncrementalPropertyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = load_rules()

    def _trace(self, ops, use_fast_path=True):
        """Fold one op at a time; fast path when possible, rebuild otherwise."""
        checkpoint = build_checkpoint([], self.rules, GAME)
        for i, op in enumerate(ops, 1):
            prefix = ops[:i]
            extended = extend_checkpoint(checkpoint, [op], self.rules, GAME) \
                if use_fast_path else None
            if extended is None:
                extended = build_checkpoint(prefix, self.rules, GAME)
                self.assertIsNotNone(extended)
            checkpoint = extended
            reference = replay(prefix, self.rules, GAME)
            mismatch = _identical(checkpoint["state"], reference)
            self.assertIsNone(
                mismatch, f"incremental diverged at prefix {i} on {mismatch}")
        return checkpoint

    def test_direct_trace_every_prefix_matches_replay(self):
        ops = [_award(i, resource="Rune essence") for i in range(1, 121)]
        checkpoint = self._trace(ops)
        self.assertEqual(checkpoint["revision"], len(ops))
        self.assertEqual(checkpoint["watermark"],
                         list(canonical_order_key(ops[-1])))

    def test_policy_one_two_gems_and_cooking_gate(self):
        ops = [
            _award(1, key=GEM_KEY, skill="mining", resource="Clay", policy=2),
            _award(2, skill="cooking", resource="Trout", policy=2),
            _award(3, skill="cooking", resource="Trout", policy=1),
            _award(4, skill="smithing", resource="Bronze bar", policy=2),
            _award(5, skill="fishing", resource="Shrimp", policy=2),
        ]
        checkpoint = self._trace(ops)
        state = checkpoint["state"]
        outcomes = state["review_outcomes"]
        reference = replay(ops, self.rules, GAME)
        self.assertEqual(state, reference)
        self.assertEqual(outcomes["rk-dev-a-2"]["outcome"], "paused_level")
        self.assertEqual(outcomes["rk-dev-a-3"]["outcome"], "practice")
        self.assertEqual(state["xp_micro"]["cooking"], 1_000_000)

    def test_gem_grant_survives_incremental_extension(self):
        game = "test-gem-inventory"
        ops = [{
            "op_id": "op-gem", "game_uuid": game, "device_id": "dev-a",
            "device_seq": 1, "lamport": 1, "kind": "review_award",
            "payload": {"review_key": GEM_KEY, "review_ts": 2000, "rating": 3,
                        "review_kind": "review", "provenance": "direct",
                        "reward_policy": 2, "skill": "mining",
                        "resource": "Clay"}}]
        checkpoint = build_checkpoint([], self.rules, game)
        extended = extend_checkpoint(checkpoint, ops, self.rules, game)
        self.assertIsNotNone(extended)
        state = extended["state"]
        self.assertEqual(state["xp_micro"]["mining"], 57_750_000)
        self.assertEqual(state["inventory"].get("Uncut sapphire"), 1)
        self.assertEqual(state["counters"]["gems"], 1)
        self.assertIn("Uncut sapphire",
                      state["review_outcomes"][GEM_KEY]["items"])
        self.assertEqual(state, replay(ops, self.rules, game))

    def test_dependent_recipes_with_catchup_and_multi_device(self):
        ops = [
            _award(1, device="dev-a", skill="fishing", resource="Shrimp"),
            {"op_id": "catch-1", "game_uuid": GAME, "device_id": "dev-b",
             "device_seq": 1, "lamport": 2, "kind": "review_award",
             "payload": {"review_key": "rk-catch", "review_ts": 2005,
                         "rating": 3, "review_kind": "review",
                         "provenance": "catchup", "reward_policy": 2}},
            _award(3, device="dev-a", skill="cooking", resource="Shrimp"),
            _award(4, device="dev-b", skill="mining", resource="Rune essence",
                   lamport=4, seq=2),
        ]
        self._trace(ops)

    def test_skips_and_duplicate_claim_fall_back_to_rebuild(self):
        _, _ = None, None
        ops = [_award(1, key="rk-x"), _skip(2, key="rk-x"), _skip(3, key="rk-y")]
        checkpoint = self._trace(ops)
        self.assertIn("rk-x", checkpoint["state"]["skipped"])
        self.assertIn("rk-y", checkpoint["state"]["skipped"])
        # A second claim for a known key invalidates the fast path (None) but
        # the reference result still wins on rebuild.
        late_direct = _award(9, key="rk-x", lamport=9, seq=9)
        self.assertIsNone(extend_checkpoint(checkpoint, [late_direct],
                                            self.rules, GAME))
        rebuilt = build_checkpoint(ops + [late_direct], self.rules, GAME)
        self.assertTrue(any("duplicate_direct_ignored" in d
                            for d in rebuilt["state"]["diagnostics"]))

    def test_retract_restore_trace(self):
        ops = [_award(1, key="rk-r"), _retract(2, "rk-r"),
               _retract(3, "rk-r", kind="review_restore")]
        checkpoint = self._trace(ops)
        state = checkpoint["state"]
        self.assertGreater(state["xp_micro"]["mining"], 0)
        # Retract alone would disable the award; the restore re-enables it.
        retracted = replay(ops[:2], self.rules, GAME)
        self.assertEqual(retracted["xp_micro"]["mining"], 0)

    def test_preset_change_invalidates_fast_path(self):
        ops = [_preset(1, "fishing", 2500),
               {"op_id": "c-1", "game_uuid": GAME, "device_id": "dev-a",
                "device_seq": 2, "lamport": 2, "kind": "review_award",
                "payload": {"review_key": "rk-c1", "review_ts": 2600,
                            "rating": 3, "review_kind": "review",
                            "provenance": "catchup", "reward_policy": 2}}]
        checkpoint = self._trace(ops)
        late_preset = _preset(9, "mining", 2400)
        late_preset["lamport"] = 9
        late_preset["device_seq"] = 9
        self.assertIsNone(extend_checkpoint(checkpoint, [late_preset],
                                            self.rules, GAME))

    def test_late_canonical_insertion_invalidates(self):
        ops = [_award(i) for i in range(1, 11)]
        checkpoint = self._trace(ops)
        late = _award(99, device="dev-z", seq=1, lamport=5, key="rk-late")
        self.assertIsNone(extend_checkpoint(checkpoint, [late], self.rules, GAME))
        rebuilt = build_checkpoint(ops + [late], self.rules, GAME)
        self.assertEqual(rebuilt["state"]["revision"], 11)

    def test_rules_and_game_change_invalidate(self):
        checkpoint = self._trace([_award(i) for i in range(1, 6)])
        changed_rules = dict(self.rules)
        changed_rules["rules_version"] = 999
        self.assertIsNone(extend_checkpoint(checkpoint, [_award(6)],
                                            changed_rules, GAME))
        self.assertIsNone(extend_checkpoint(checkpoint, [_award(6)],
                                            self.rules, "other-game"))
        stale = dict(checkpoint)
        stale["reducer_version"] = REDUCER_VERSION - 1
        self.assertIsNone(extend_checkpoint(stale, [_award(6)], self.rules, GAME))
        stale2 = dict(checkpoint)
        stale2["version"] = CHECKPOINT_VERSION + 1
        self.assertIsNone(extend_checkpoint(stale2, [_award(6)], self.rules, GAME))

    def test_arrival_order_invariance_with_fixed_canonical_fields(self):
        ops = [_award(i) for i in range(1, 31)]
        shuffled = list(ops)
        random.Random(1234).shuffle(shuffled)
        self.assertEqual(replay(ops, self.rules, GAME),
                         replay(shuffled, self.rules, GAME))
        incremental = build_checkpoint(ops, self.rules, GAME)["state"]
        self.assertEqual(incremental, replay(shuffled, self.rules, GAME))

    def test_unsupported_policy_blocks_fast_path(self):
        checkpoint = build_checkpoint([], self.rules, GAME)
        bad = _award(1)
        bad["payload"]["reward_policy"] = 7
        self.assertIsNone(extend_checkpoint(checkpoint, [bad], self.rules, GAME))

    def test_equivalence_over_random_traces(self):
        rng = random.Random(20260910)
        skills = [("mining", "Rune essence"), ("mining", "Clay"),
                  ("woodcutting", "Tree"), ("fishing", "Shrimp"),
                  ("cooking", "Shrimp"), ("cooking", "Trout"),
                  ("smithing", "Bronze bar"), ("crafting", "Gold ring")]
        for trace_index in range(12):
            ops = []
            for i in range(1, 51):
                roll = rng.random()
                if roll < 0.75:
                    skill, resource = rng.choice(skills)
                    ops.append(_award(i, skill=skill, resource=resource,
                                      policy=rng.choice((1, 2))))
                elif roll < 0.85:
                    ops.append(_award(i, prov="catchup",
                                      resource=rng.choice(skills)[1]))
                elif roll < 0.92:
                    ops.append(_skip(i, key=f"rk-skip-{i}"))
                else:
                    target = f"rk-dev-a-{max(1, i - 1)}"
                    kind = "review_restore" if rng.random() < 0.5 else "review_retract"
                    ops.append(_retract(i, target, kind=kind))
            self._trace(ops)

    def test_checkpoint_metadata_is_versioned(self):
        checkpoint = build_checkpoint([_award(1)], self.rules, GAME)
        self.assertEqual(checkpoint["version"], CHECKPOINT_VERSION)
        self.assertEqual(checkpoint["reducer_version"], REDUCER_VERSION)
        self.assertEqual(checkpoint["rules_hash"], rules_hash(self.rules))
        self.assertEqual(checkpoint["game_uuid"], GAME)
        self.assertEqual(checkpoint["known_keys"], ["rk-dev-a-1"])


if __name__ == "__main__":
    unittest.main()
