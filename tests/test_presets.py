import os
import tempfile
import unittest

from evolved.data import load_rules
from evolved.engine import EngineConfig, EvolvedEngine
from evolved.journal import Journal
from evolved.presets import apply_preset, current_preset
from evolved.reducer import replay


class _Col:
    def __init__(self):
        self.store = {}

    def get_config(self, key, default=None):
        return self.store.get(key, default)

    def set_config(self, key, value):
        self.store[key] = value


def _engine(path, game="game-preset-1"):
    journal = Journal(path)
    cfg = EngineConfig(game_uuid=game, device_id="dev-a", activated_at=1000,
                       rules=load_rules())
    return EvolvedEngine(cfg, journal), journal


class TestPresets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_default_mining_then_fishing(self):
        eng, journal = _engine(os.path.join(self.tmp.name, "p.sqlite3"))
        self.addCleanup(journal.close)
        col = _Col()
        self.assertEqual(current_preset(col), "mining")
        out = apply_preset(eng, journal, col, "fishing", now_ts=5000)
        self.assertTrue(out["ok"])
        self.assertEqual(current_preset(col), "fishing")
        # Catch-up before the preset resolves to Mining, after to Fishing.
        ops = [
            {"op_id": "op-early", "game_uuid": eng.cfg.game_uuid,
             "device_id": "dev-a", "device_seq": 10, "lamport": 10,
             "kind": "review_award",
             "payload": {"review_key": "rk-early", "review_ts": 4000,
                         "rating": 3, "review_kind": "review",
                         "provenance": "catchup"}},
            {"op_id": "op-late", "game_uuid": eng.cfg.game_uuid,
             "device_id": "dev-a", "device_seq": 12, "lamport": 12,
             "kind": "review_award",
             "payload": {"review_key": "rk-late", "review_ts": 6000,
                         "rating": 3, "review_kind": "review",
                         "provenance": "catchup"}},
        ]
        for op in ops:
            journal.append_operation(op)
        state = replay(
            ops + [{"op_id": "op-preset", "game_uuid": eng.cfg.game_uuid,
                    "device_id": "dev-a", "device_seq": 11, "lamport": 11,
                    "kind": "catchup_preset",
                    "payload": {"skill": "fishing", "effective_ts": 5000}}],
            load_rules(), eng.cfg.game_uuid)
        self.assertGreater(state["xp_micro"]["mining"], 0)
        self.assertGreater(state["xp_micro"]["fishing"], 0)

    def test_bad_preset_rejected(self):
        eng, journal = _engine(os.path.join(self.tmp.name, "q.sqlite3"))
        self.addCleanup(journal.close)
        with self.assertRaises(ValueError):
            apply_preset(eng, journal, _Col(), "smithing")


if __name__ == "__main__":
    unittest.main()
