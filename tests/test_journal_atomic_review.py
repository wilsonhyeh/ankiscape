# tests/test_journal_atomic_review.py - Atomic accepted-answer persistence.
"""Falsifies the durability contract: sequence + operation + outbox +
observation commit together; duplicates reuse committed identity; changed
payloads conflict; a failed interactive transaction neither marks the review
processed nor advances the sequence; the interactive busy budget is short.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest

from evolved.journal import (
    DEFAULT_BUSY_MS, INTERACTIVE_BUSY_MS, Journal, payload_hash,
)


def _op(op_id="op-1", seq=1, lamport=1, kind="review_award", device="dev-a",
        payload=None):
    return {"op_id": op_id, "game_uuid": "game-1", "device_id": device,
            "device_seq": seq, "lamport": lamport, "kind": kind,
            "payload": payload or {"review_key": "rk-1", "skill": "mining",
                                   "resource": "Rune essence", "provenance": "direct"}}


class AtomicReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "game.sqlite3")
        self.j = Journal(self.path)
        self.addCleanup(self.j.close)

    def _record(self, op, key="rk-1", revlog=11, card=21):
        return self.j.record_review(op, review_key=key, revlog_id=revlog,
                                    card_id=card, fingerprint=f"fp-{key}")

    def test_sequence_operation_outbox_observation_commit_together(self):
        op = _op()
        out = self._record(op)
        self.assertTrue(out["ok"])
        self.assertFalse(out["duplicate"])
        self.assertEqual(out["device_seq"], 1)
        ops = self.j.all_operations()
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["device_seq"], 1)
        self.assertEqual(len(self.j.pending_operations()), 1)
        self.assertEqual(self.j.review_observations()[0]["review_key"], "rk-1")
        self.assertEqual(out["device_seq"],
                         self.j.pending_operations()[0]["device_seq"])

    def test_duplicate_identical_returns_existing_identity(self):
        first = self._record(_op())
        second = self._record(_op())
        self.assertTrue(second["ok"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["device_seq"], first["device_seq"])
        self.assertEqual(second["op_id"], first["op_id"])
        self.assertEqual(self.j.operation_count(), 1)
        self.assertEqual(len(self.j.review_observations()), 1)

    def test_changed_payload_reusing_op_id_conflicts(self):
        self._record(_op())
        changed = _op(payload={"review_key": "rk-1-CHANGED"})
        out = self._record(changed)
        self.assertFalse(out["ok"])
        self.assertTrue(out["conflict"])
        # The committed operation is untouched; no duplicate observation.
        self.assertEqual(self.j.operation_count(), 1)
        self.assertNotEqual(
            self.j.find_operation_payload("op-1"),
            {"review_key": "rk-1-CHANGED"})
        self.assertEqual(self.j.review_observations()[0]["review_key"], "rk-1")

    def test_observation_is_atomic_with_operation(self):
        out = self._record(_op("op-a"), key="rk-a", revlog=1, card=2)
        obs = {row["review_key"]: row for row in self.j.review_observations()}
        self.assertIn("rk-a", obs)
        self.assertEqual(obs["rk-a"]["revlog_id"], 1)
        # A conflicting duplicate observation for the same op never lands.
        self._record(_op("op-a"), key="rk-a", revlog=999, card=999)
        obs2 = self.j.review_observations()
        self.assertEqual(len([o for o in obs2 if o["review_key"] == "rk-a"]), 1)
        self.assertEqual(
            [o for o in obs2 if o["review_key"] == "rk-a"][0]["revlog_id"], 1)
        self.assertTrue(out["ok"])

    def test_busy_write_fails_fast_without_advancing_sequence(self):
        blocker = sqlite3.connect(self.path, timeout=1.0, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            blocker.execute(
                "INSERT INTO operations(op_id, game_uuid, device_id, device_seq,"
                " lamport, kind, payload_json, payload_hash, created_at, acked)"
                " VALUES('blocker','game-1','dev-a',1,1,'review_award','{}','h',0,0)")
            started = time.monotonic()
            out = self._record(_op("op-busy", seq=99, lamport=99))
            elapsed = time.monotonic() - started
            self.assertFalse(out["ok"])
            self.assertTrue(out["busy"])
            # Interactive budget is tiny; anything near the old 30 s default
            # is a regression. Allow generous CI slack but keep the bound.
            self.assertLess(elapsed, 1.0)
            self.assertLess(INTERACTIVE_BUSY_MS, DEFAULT_BUSY_MS)
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        # After the lock clears the next review commits with the next real
        # sequence (the failed transaction allocated nothing durable).
        follow = self._record(_op("op-next"), key="rk-next", revlog=12, card=22)
        self.assertTrue(follow["ok"])
        self.assertEqual(follow["device_seq"], 1)
        self.assertEqual(self.j.operation_count(), 1)

    def test_concurrent_records_get_unique_sequences(self):
        errors = []

        def worker(start):
            try:
                for i in range(start, start + 20):
                    out = self.j.record_review(
                        _op(f"op-{i}", lamport=i),
                        review_key=f"rk-{i}", revlog_id=i, card_id=i,
                        fingerprint=f"fp-{i}")
                    if not out.get("ok"):
                        errors.append(out)
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(base,))
                   for base in (100, 200, 300)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        ops = self.j.all_operations()
        self.assertEqual(len(ops), 60)
        seqs = sorted(o["device_seq"] for o in ops)
        self.assertEqual(seqs, list(range(1, 61)))
        self.assertEqual(len(self.j.operation_ids()), 60)

    def test_reopen_after_commit_keeps_committed_review(self):
        self._record(_op())
        self.j.close()
        reopened = Journal(self.path)
        try:
            self.assertEqual(reopened.operation_count(), 1)
            self.assertEqual(reopened.review_observations()[0]["review_key"], "rk-1")
            self.assertEqual(len(reopened.pending_operations()), 1)
        finally:
            reopened.close()

    def test_payload_hash_matches_append_operation(self):
        op = _op()
        out = self._record(op)
        self.assertEqual(out["payload_hash"], payload_hash(op["payload"]))


if __name__ == "__main__":
    unittest.main()
