import os
import sqlite3
import tempfile
import unittest

from evolved.journal import (
    Journal, build_sync_pointer, journal_path_for_profile, payload_hash,
)


class TestJournal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "g1", "game.sqlite3")
        self.j = Journal(self.path)

    def tearDown(self):
        try:
            self.j.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def _op(self, op_id="op-1", seq=1, lamport=1, kind="review_award", payload=None):
        return {"op_id": op_id, "game_uuid": "game-1", "device_id": "dev-a",
                "device_seq": seq, "lamport": lamport, "kind": kind,
                "payload": payload or {"review_key": "rk1"}}

    def test_append_persists_before_award_and_survives_reopen(self):
        self.j.append_operation(self._op())
        pending = self.j.pending_operations()
        self.assertEqual(len(pending), 1)
        self.j.close()
        j2 = Journal(self.path)
        try:
            pending2 = j2.pending_operations()
            self.assertEqual(len(pending2), 1)
            j2.mark_acked(["op-1"])
            self.assertEqual(j2.pending_operations(), [])
        finally:
            j2.close()

    def test_duplicate_op_id_and_seq_conflict(self):
        self.j.append_operation(self._op("op-1", seq=1))
        with self.assertRaises(Exception):
            self.j.append_operation(self._op("op-1", seq=1))
        with self.assertRaises(Exception):
            # Same (device, seq), different payload must not overwrite.
            self.j.append_operation(self._op("op-2", seq=1, payload={"review_key": "rk2"}))
        # Unacknowledged work is preserved (crash/restart keeps it).
        self.assertEqual(len(self.j.pending_operations()), 1)

    def test_backup_and_pointer_bound(self):
        self.j.append_operation(self._op())
        dest = os.path.join(self.tmp.name, "backup.sqlite3")
        self.j.backup_to(dest)
        self.assertTrue(os.path.exists(dest))
        pointer = build_sync_pointer(game_uuid="game-1", activated_at=123,
                                     snapshot_revision=4, preset_skill="mining",
                                     preset_effective_ts=120)
        import json

        self.assertLessEqual(len(json.dumps(pointer).encode()), 8 * 1024)
        self.assertEqual(payload_hash({"a": 1}), payload_hash({"a": 1}))

    def test_future_schema_rejected(self):
        self.j.close()
        conn = sqlite3.connect(self.path)
        conn.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
        conn.commit()
        conn.close()
        with self.assertRaises(RuntimeError):
            Journal(self.path)

    def test_profile_path_layout(self):
        p = journal_path_for_profile("/prof", "game-9")
        self.assertIn(os.path.join("ankiscape-evolved", "game-9", "game.sqlite3"), p)


if __name__ == "__main__":
    unittest.main()
