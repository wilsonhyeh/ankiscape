import json
import os
import unittest
from fractions import Fraction

from evolved.data import by_display, load_rules
from evolved.draws import TWO_POW_48, canonical_draw_input, draw_hits, draw_r, frac_hits
from evolved.logic_pure import (
    MICRO, burn_probability, eval_cook, eval_gather, eval_production,
    gathering_probability, level_from_xp_micro, mul_half_up, multiplied_base_micro,
    tier_multiplier,
)


def _rules():
    return load_rules()


class TestDraws(unittest.TestCase):
    def test_canonical_bytes_no_spaces(self):
        raw = canonical_draw_input(1, "game-1", "abc123", "action")
        self.assertEqual(raw, b'[1,"game-1","abc123","action"]')

    def test_deterministic_and_bounded(self):
        r1 = draw_r(1, "game-1", "abc123", "action")
        r2 = draw_r(1, "game-1", "abc123", "action")
        self.assertEqual(r1, r2)
        self.assertGreaterEqual(r1, 0)
        self.assertLess(r1, TWO_POW_48)

    def test_exact_rational_compare(self):
        # r=0 always hits any positive probability; max r never hits p<1.
        self.assertTrue(draw_hits(0, 1, 256))
        self.assertFalse(draw_hits(TWO_POW_48 - 1, 1, 256))
        self.assertTrue(frac_hits(0, Fraction(1, 4)))
        with self.assertRaises(ValueError):
            canonical_draw_input(1, "game 1!", "abc", "action")
        with self.assertRaises(ValueError):
            canonical_draw_input(1, "g", "abc", "nope")


class TestEconomy(unittest.TestCase):
    def test_rules_load_and_frozen_tiers(self):
        rules = _rules()
        self.assertEqual(rules["rules_version"], 1)
        self.assertEqual([o["tier"] for o in rules["ores"]], list(range(1, 12)))
        self.assertEqual([t["tier"] for t in rules["trees"]], list(range(1, 10)))
        soft = by_display(rules["crafting"], "Soft clay")
        self.assertEqual(soft["base_xp"], 1)
        shrimp = next(f for f in rules["fish"] if f["id"] == "shrimp")
        self.assertEqual(shrimp["cooking_level"], 1)

    def test_redwood_532_and_fail_quarter(self):
        # 380 base x tier9 (1.40) = 532 XP; fail = 133 XP.
        self.assertEqual(multiplied_base_micro(380, 9), 532 * MICRO)
        ok = eval_gather(skill="woodcutting", resource_display="Redwood",
                         resource_tier=9, base_xp=380, player_level=90, success=True)
        self.assertEqual(ok.xp_micro, 532 * MICRO)
        self.assertEqual(ok.item_out, "Redwood")
        fail = eval_gather(skill="woodcutting", resource_display="Redwood",
                           resource_tier=9, base_xp=380, player_level=90, success=False)
        self.assertEqual(fail.xp_micro, 133 * MICRO)
        self.assertIsNone(fail.item_out)

    def test_failed_gather_minimum_1xp(self):
        # Tiny base still floors at 1 XP on failure.
        fail = eval_gather(skill="mining", resource_display="Rune essence",
                           resource_tier=1, base_xp=0.01, player_level=1, success=False)
        self.assertGreaterEqual(fail.xp_micro, MICRO)

    def test_golden_vectors(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "shared", "golden-vectors.json")
        with open(path, encoding="utf-8") as fh:
            gold = json.load(fh)
        by_case = {v["case"]: v for v in gold["xp_vectors"] if "expected_xp_micro" in v}
        self.assertEqual(multiplied_base_micro(380, 9), int(by_case["redwood_success_no_gem"]["expected_xp_micro"]))
        self.assertEqual(multiplied_base_micro(13, 1), int(by_case["shrimp_cook_success"]["expected_xp_micro"]))
        burn = eval_cook(fish_display="Shrimp", fish_tier=1, cooking_base_xp=13,
                         player_level=1, burned=True, has_fish=True)
        self.assertEqual(burn.xp_micro, int(by_case["shrimp_cook_burn"]["expected_xp_micro"]))
        self.assertIsNone(burn.item_out)
        practice = eval_production(skill="smithing", output_display="Bronze bar",
                                   output_tier=1, base_xp=6.2, level_ok=True,
                                   materials_ok=False, requirements={"Copper ore": 1})
        self.assertEqual(practice.xp_micro, MICRO)
        self.assertIsNone(practice.item_out)

    def test_burn_probability_level99(self):
        self.assertEqual(burn_probability(99), Fraction(102, 1000))
        self.assertEqual(burn_probability(200), Fraction(0, 1))

    def test_level_thresholds(self):
        rules = _rules()
        th = rules["thresholds"]
        self.assertEqual(level_from_xp_micro(0, th), 1)
        self.assertEqual(level_from_xp_micro(82 * MICRO, th), 1)
        self.assertEqual(level_from_xp_micro(83 * MICRO, th), 2)
        self.assertEqual(level_from_xp_micro(13034431 * MICRO, th), 99)
        self.assertEqual(level_from_xp_micro(10 ** 15, th), 99)

    def test_round_half_up_not_bankers(self):
        # 6.2 XP tier1: exact 6_200_000 micro. Half-up check: 0.5 micro rounds up.
        self.assertEqual(mul_half_up(1, Fraction(1, 2)), 1)
        self.assertEqual(tier_multiplier(1), Fraction(1, 1))
        self.assertEqual(tier_multiplier(9), Fraction(140, 100))
        self.assertEqual(tier_multiplier(100), Fraction(2, 1))

    def test_gem_xp_gets_ore_multiplier(self):
        res = eval_gather(skill="mining", resource_display="Coal", resource_tier=7,
                          base_xp=50, player_level=30, success=True,
                          gem={"base_xp": 50}, gem_ore_mult_tier=7)
        # 50 x 1.30 = 65 each -> 130 total.
        self.assertEqual(res.xp_micro, 130 * MICRO)

    def test_all_skills_reachable_from_fresh(self):
        rules = _rules()
        # Every skill has a level-1 entry (starter recipe exists).
        self.assertTrue(any(o["level"] == 1 for o in rules["ores"]))
        self.assertTrue(any(t["level"] == 1 for t in rules["trees"]))
        self.assertTrue(any(f["fishing_level"] == 1 for f in rules["fish"]))
        self.assertTrue(any(f["cooking_level"] == 1 for f in rules["fish"]))
        self.assertTrue(any(b["level"] == 1 for b in rules["bars"]))
        self.assertTrue(any(c["level"] == 1 for c in rules["crafting"]))
        # Gathering probability bounded.
        p = gathering_probability(99, 0.5)
        self.assertLessEqual(float(p), 0.95 * 0.5 + 1e-9)


if __name__ == "__main__":
    unittest.main()
