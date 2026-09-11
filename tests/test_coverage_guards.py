# tests/test_coverage_guards.py - Focused guards owned by the mutation gate.
"""Each test here must fail when its guard is removed (dev/mutation_gate.py
injects the defect into a temp copy and asserts the owning test fails)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evolved.data import load_rules  # noqa: E402
from evolved.engine import EngineConfig, EvolvedEngine  # noqa: E402
from evolved.journal import Journal  # noqa: E402
from evolved.ui.stale import response_is_stale  # noqa: E402


class PolicyGuardTests(unittest.TestCase):
    def test_engine_rejects_unknown_reward_policy(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        journal = Journal(os.path.join(tmp.name, "g.sqlite3"))
        self.addCleanup(journal.close)
        engine = EvolvedEngine(
            EngineConfig(game_uuid="g-guard", device_id="dev",
                         activated_at=0, rules=load_rules()), journal)
        result = engine.credit_direct(
            revlog_id=1, card_id=2, ease=3, revlog_type=1, review_ts=2000,
            skill="mining", resource="Rune essence", reward_policy=7)
        self.assertFalse(result["ok"])
        self.assertIn("unsupported_reward_policy", result["error"])
        self.assertEqual(journal.operation_count(), 0)


class StaleGuardTests(unittest.TestCase):
    def test_stale_when_closed(self):
        self.assertTrue(response_is_stale(closed=True, request_id=1,
                                          current_request_id=1))

    def test_stale_when_superseded(self):
        self.assertTrue(response_is_stale(closed=False, request_id=1,
                                          current_request_id=2))

    def test_current_response_is_not_stale(self):
        self.assertFalse(response_is_stale(closed=False, request_id=2,
                                           current_request_id=2))


if __name__ == "__main__":
    unittest.main()
