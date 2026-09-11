# tests/test_projection_worker.py - Off-thread projection worker contract.
"""The worker owns its own connection, publishes only completed immutable
projections, extends the checkpoint on the fast path, rebuilds on
invalidating operations, keeps the last good state on failure, and stops
with a bounded join."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
import uuid

from evolved.data import load_rules
from evolved.engine import EngineConfig, EvolvedEngine, loading_projection
from evolved.journal import Journal
from evolved.projection_worker import ProjectionWorker
from evolved.reducer import replay


def _wait_for(predicate, timeout=8.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _seed_ops(game, count):
    ops, observations = [], []
    for i in range(1, count + 1):
        key = f"rk-seed-{i}"
        ops.append({"op_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"seed:{i}")),
                    "game_uuid": game, "device_id": "dev-seed", "device_seq": i,
                    "lamport": i, "kind": "review_award",
                    "payload": {"review_key": key, "review_ts": 2000 + i,
                                "rating": 3, "review_kind": "review",
                                "provenance": "direct", "reward_policy": 2,
                                "skill": "mining", "resource": "Rune essence"}})
        observations.append({"review_key": key, "revlog_id": 900000 + i,
                             "card_id": 800000 + i, "fingerprint": key})
    return ops, observations


class ProjectionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "game.sqlite3")
        self.rules = load_rules()
        self.game = "game-worker-1"
        self.journal = Journal(self.path)
        self.addCleanup(self.journal.close)
        ops, observations = _seed_ops(self.game, 40)
        self.journal.import_game(self.game, {"operations": ops,
                                             "observations": observations})
        self.cfg = EngineConfig(game_uuid=self.game, device_id="dev-w",
                                activated_at=0, rules=self.rules)
        self.workers = []

    def _worker(self, **kwargs):
        kwargs.setdefault("checkpoint_every", 1)
        kwargs.setdefault("checkpoint_interval", 0.0)
        worker = ProjectionWorker(self.path, self.cfg, **kwargs).start()
        self.workers.append(worker)
        self.addCleanup(lambda: worker.stop(timeout=2.0))
        return worker

    def test_publishes_full_projection_for_existing_history(self):
        worker = self._worker()
        worker.notify_dirty()
        self.assertTrue(_wait_for(lambda: worker.latest() is not None))
        latest = worker.latest()
        reference = replay(self.journal.all_operations(), self.rules, self.game)
        self.assertEqual(latest["revision"], reference["revision"])
        self.assertEqual(latest["xp_micro"], reference["xp_micro"])
        self.assertEqual(latest["inventory"], reference["inventory"])
        status = worker.status()
        self.assertFalse(status["busy"])
        self.assertEqual(status["failed"], "")

    def test_engine_pending_credit_finalizes_exactly_once(self):
        worker = self._worker()
        engine = EvolvedEngine(self.cfg, self.journal)
        engine.hydrate()
        engine.attach_worker(worker)
        worker.notify_dirty()
        self.assertTrue(_wait_for(lambda: worker.latest() is not None))
        result = engine.credit_direct(revlog_id=5001, card_id=5002, ease=3,
                                      revlog_type=1, review_ts=3000,
                                      skill="mining", resource="Rune essence",
                                      reward_policy=2)
        self.assertTrue(result["ok"])
        if result.get("pending"):
            self.assertIsNone(result["awarded"])
            self.assertTrue(_wait_for(
                lambda: engine.outcome_for(result["review_key"]) is not None))
            outcome = engine.outcome_for(result["review_key"])
        else:
            outcome = {"rewarded": result["awarded"]}
        self.assertTrue(outcome.get("rewarded"))
        latest = engine.projection()
        reference = replay(self.journal.all_operations(), self.rules, self.game)
        self.assertEqual(latest["revision"], reference["revision"])
        self.assertEqual(latest["xp_micro"], reference["xp_micro"])
        self.assertIn(result["review_key"], latest["review_outcomes"])
        self.assertFalse(latest.get("loading"))

    def test_retraction_triggers_rebuild_and_removes_award(self):
        worker = self._worker()
        engine = EvolvedEngine(self.cfg, self.journal)
        engine.hydrate()
        engine.attach_worker(worker)
        worker.notify_dirty()
        self.assertTrue(_wait_for(lambda: worker.latest() is not None))
        before = worker.latest()["xp_micro"]["mining"]
        self.assertGreater(before, 0)
        engine.retract(review_key="rk-seed-1")
        self.assertTrue(_wait_for(
            lambda: worker.latest()["total_level"] is not None
            and worker.latest()["revision"] >= 41))
        after = worker.latest()
        reference = replay(self.journal.all_operations(), self.rules, self.game)
        self.assertEqual(after, reference)
        self.assertLessEqual(after["xp_micro"]["mining"], before)

    def test_loading_state_before_first_publish(self):
        worker = ProjectionWorker(self.path, self.cfg)
        self.workers.append(worker)
        engine = EvolvedEngine(self.cfg, self.journal, worker=worker)
        projection = engine.projection()
        self.assertTrue(projection.get("loading"))
        self.assertEqual(projection, loading_projection())
        worker.start()
        worker.notify_dirty()
        self.assertTrue(_wait_for(lambda: not engine.projection().get("loading")))

    def test_checkpoint_persists_and_reuses(self):
        worker = self._worker()
        worker.notify_dirty()
        self.assertTrue(_wait_for(lambda: worker.latest() is not None))
        worker.flush_checkpoint()
        checkpoint = self.journal.load_checkpoint(self.game)
        self.assertIsNotNone(checkpoint)
        self.assertEqual(checkpoint["revision"],
                         self.journal.operation_count())
        self.assertTrue(worker.stop(timeout=2.0))
        # A fresh worker with the persisted checkpoint produces the same state.
        worker2 = self._worker()
        worker2.notify_dirty()
        self.assertTrue(_wait_for(lambda: worker2.latest() is not None))
        self.assertEqual(worker2.latest(), worker.latest())

    def test_corrupt_checkpoint_is_disposable(self):
        worker = self._worker()
        worker.notify_dirty()
        self.assertTrue(_wait_for(lambda: worker.latest() is not None))
        worker.flush_checkpoint()
        worker.stop(timeout=2.0)
        conn = sqlite3.connect(self.path)
        conn.execute("UPDATE projection_checkpoints SET state_json='{broken'")
        conn.commit()
        conn.close()
        worker2 = self._worker()
        worker2.notify_dirty()
        self.assertTrue(_wait_for(lambda: worker2.latest() is not None))
        reference = replay(self.journal.all_operations(), self.rules, self.game)
        self.assertEqual(worker2.latest(), reference)
        self.assertEqual(worker2.status()["failed"], "")

    def test_failure_keeps_last_good_state_and_reports_status(self):
        blocker = os.path.join(self.tmp.name, "not-a-directory")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("blocking file; a path cannot be created under it")
        worker = ProjectionWorker(os.path.join(blocker, "game.sqlite3"),
                                  self.cfg)
        self.workers.append(worker)
        worker.start()
        self.assertTrue(_wait_for(lambda: worker.status()["failed"] != ""))
        self.assertIsNone(worker.latest())
        self.assertTrue(worker.stop(timeout=2.0))

    def test_failed_rebuild_never_replaces_published_state(self):
        worker = self._worker()
        worker.notify_dirty()
        self.assertTrue(_wait_for(lambda: worker.latest() is not None))
        good = worker.latest()
        # Break the fast path AND the rebuild source by pointing at a
        # missing game file: simulate by closing the worker's reader through
        # a path that disappears. Use a copied journal instead.
        worker.stop(timeout=2.0)
        moved = os.path.join(self.tmp.name, "moved.sqlite3")
        os.rename(self.path, moved)
        worker2 = ProjectionWorker(self.path, self.cfg)
        self.workers.append(worker2)
        worker2._latest = good  # last good publication survives
        worker2.notify_dirty()
        worker2.start()
        worker2.notify_dirty()
        self.assertTrue(_wait_for(lambda: worker2.status()["failed"] != ""))
        self.assertEqual(worker2.latest(), good)
        self.assertTrue(worker2.stop(timeout=2.0))

    def test_stop_is_bounded_and_idempotent(self):
        worker = self._worker()
        worker.notify_dirty()
        self.assertTrue(_wait_for(lambda: worker.latest() is not None))
        started = time.monotonic()
        self.assertTrue(worker.stop(timeout=2.0))
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(worker.stop(timeout=2.0))

    def test_worker_uses_its_own_connection(self):
        worker = self._worker()
        worker.notify_dirty()
        self.assertTrue(_wait_for(lambda: worker.latest() is not None))
        # The caller journal stays usable concurrently (no shared handle).
        self.journal.record_review(
            {"op_id": "op-live", "game_uuid": self.game, "device_id": "dev-w",
             "lamport": 999, "kind": "review_award",
             "payload": {"review_key": "rk-live", "review_ts": 4000,
                         "rating": 3, "review_kind": "review",
                         "provenance": "direct", "reward_policy": 2,
                         "skill": "mining", "resource": "Rune essence"}},
            review_key="rk-live", revlog_id=999, card_id=999,
            fingerprint="fp-live")
        worker.notify_dirty()
        self.assertTrue(_wait_for(
            lambda: (worker.latest() or {}).get("revision", 0) >= 41))
        self.assertIn("rk-live", worker.latest()["review_outcomes"])


if __name__ == "__main__":
    unittest.main()
