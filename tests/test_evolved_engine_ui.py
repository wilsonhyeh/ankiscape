import os
import tempfile
import unittest

from evolved.data import load_rules
from evolved.engine import EngineConfig, EvolvedEngine
from evolved.journal import Journal
from evolved.ui.bank import filter_inventory
from evolved.ui.chooser import show_mode_chooser
from evolved.ui.hud import HudOwner


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(os.path.join(self.tmp.name, "game.sqlite3"))
        self.addCleanup(self.journal.close)
        self.rules = load_rules()
        cfg = EngineConfig(game_uuid="game-eng-1", device_id="dev-a",
                           activated_at=1000, rules=self.rules)
        self.eng = EvolvedEngine(cfg, self.journal)

    def test_direct_award_idempotent_and_persisted(self):
        r1 = self.eng.credit_direct(revlog_id=11, card_id=21, ease=3, revlog_type=1,
                                    review_ts=1100, skill="mining", resource="Rune essence")
        self.assertTrue(r1["awarded"])
        r2 = self.eng.credit_direct(revlog_id=11, card_id=21, ease=3, revlog_type=1,
                                    review_ts=1100, skill="mining", resource="Rune essence")
        self.assertFalse(r2["awarded"])
        self.assertEqual(r2["reason"], "duplicate_delivery")
        # Rating 1 never awards.
        r3 = self.eng.credit_direct(revlog_id=12, card_id=22, ease=1, revlog_type=1,
                                    review_ts=1100, skill="mining", resource="Rune essence")
        self.assertFalse(r3["awarded"])

    def test_undo_retract_restore(self):
        r = self.eng.credit_direct(revlog_id=31, card_id=32, ease=3, revlog_type=1,
                                   review_ts=1200, skill="woodcutting", resource="Tree")
        key = r["review_key"]
        self.assertTrue(self.eng.retract(review_key=key)["ok"])
        self.assertTrue(self.eng.restore(review_key=key)["ok"])

    def test_catchup_chunked(self):
        history = [{"revlog_id": i, "card_id": i, "ease": 3, "revlog_type": 1,
                    "ts": 1100 + i, "review_key": f"ck-{i}"} for i in range(1, 8)]
        out = self.eng.scan_catchup(history, chunk=3)
        self.assertEqual(out["made"], 3)


class TestEvolvedUI(unittest.TestCase):
    def test_bank_filters(self):
        inv = {"Rune essence": 5, "Tree": 0, "Bronze bar": 2, "Shrimp": 4,
               "Cooked Shrimp": 1, "Gold ring": 0}
        self.assertEqual(filter_inventory(inv, skill="smithing"), [("Bronze bar", 2)])
        self.assertIn(("Shrimp", 4), filter_inventory(inv, query="shr"))
        self.assertNotIn(("Tree", 0), filter_inventory(inv))

    def test_chooser_close_is_classic(self):
        store = {}

        def get():
            return store.get("mode", "classic")

        def set_(v):
            store["mode"] = v

        self.assertEqual(show_mode_chooser(get_requested=get, set_requested=set_,
                                           qt_dialog=lambda: None), "classic")
        self.assertEqual(store["mode"], "classic")
        self.assertEqual(show_mode_chooser(get_requested=get, set_requested=set_,
                                           qt_dialog=lambda: "evolved"), "evolved")

    def test_hud_coalesced_single_paint(self):
        paints = []
        hud = HudOwner(apply=lambda s: paints.append((s.skill, s.level)))
        hud.request_update(skill="fishing", level=5)
        hud.request_update(skill="fishing", level=6)
        self.assertTrue(hud.pump())
        self.assertFalse(hud.pump())
        self.assertEqual(len(paints), 1)
        self.assertEqual(paints[0][1], 6)
        hud.release()


if __name__ == "__main__":
    unittest.main()
