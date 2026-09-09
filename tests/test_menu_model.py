import unittest

from evolved.data import load_rules
from evolved.ui.menu_model import (
    achievement_rows, format_hiscores_rows, hiscores_status, skill_rows,
    validate_preset,
)


class TestMenuModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = load_rules()

    def test_six_skill_rows_offline(self):
        rows = skill_rows(self.rules, {}, {}, {}, {})
        self.assertEqual([r["skill"] for r in rows],
                         ["mining", "woodcutting", "smithing", "crafting",
                          "fishing", "cooking"])
        # Gathering rows are always ready; production without selection is not.
        by_skill = {r["skill"]: r for r in rows}
        self.assertTrue(by_skill["mining"]["materials"]["ready"])
        self.assertFalse(by_skill["smithing"]["materials"]["ready"])

    def test_production_materials_check(self):
        rows = skill_rows(self.rules, {"smithing": 1}, {"smithing": 0},
                          {"Copper ore": 1, "Tin ore": 1},
                          {"smithing": "Bronze bar"})
        bronze = next(r for r in rows if r["skill"] == "smithing")
        self.assertTrue(bronze["materials"]["ready"])
        rows2 = skill_rows(self.rules, {"smithing": 1}, {"smithing": 0},
                           {"Copper ore": 1},
                           {"smithing": "Bronze bar"})
        bronze2 = next(r for r in rows2 if r["skill"] == "smithing")
        self.assertFalse(bronze2["materials"]["ready"])
        self.assertIn("gather", bronze2["materials"]["note"])

    def test_achievement_and_hiscores_helpers(self):
        required = self.rules["achievements"]["required"]
        rows = achievement_rows(required, ["first_catch"])
        self.assertEqual(len(rows), len(required))
        self.assertTrue(next(r for r in rows if r["id"] == "first_catch")["unlocked"])
        self.assertEqual(format_hiscores_rows(
            [{"rank": 1, "username": "W", "xp": 500}])[0]["rank"], 1)
        self.assertIn("offline", hiscores_status(logged_in=False, last_success=None,
                                                 pending=3))
        self.assertIn("pending 3", hiscores_status(logged_in=True, last_success=None,
                                                   pending=3))
        self.assertIn("problem", hiscores_status(logged_in=True, last_success=None,
                                                 pending=0, last_error="boom"))

    def test_preset_validation(self):
        self.assertEqual(validate_preset("Fishing"), "fishing")
        with self.assertRaises(ValueError):
            validate_preset("smithing")


if __name__ == "__main__":
    unittest.main()
