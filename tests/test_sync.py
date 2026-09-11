import os
import tempfile
import unittest

from evolved.journal import Journal
from evolved.sync import SyncJob, SyncTriggers


class _Net:
    def __init__(self):
        self.uploaded = []
        self.pages = [{"status": "ok", "operations": [], "next_cursor": "0"}]

    def upload(self, batch):
        self.uploaded.append(list(batch))
        return {"status": "ok", "acked": [op["op_id"] for op in batch]}

    def download(self, cursor):
        return dict(self.pages[0])


class TestSyncJob(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(os.path.join(self.tmp.name, "game.sqlite3"))
        self.addCleanup(self.journal.close)
        self.net = _Net()

    def _op(self, op_id, seq):
        return {"op_id": op_id, "game_uuid": "game-sync-1", "device_id": "dev-a",
                "device_seq": seq, "lamport": seq, "kind": "review_award",
                "payload": {"review_key": f"rk-{op_id}"}}

    def _job(self):
        applied = []
        return SyncJob(generation=7, game_uuid="game-sync-1", user_id="u1",
                       journal=self.journal, upload=self.net.upload,
                       download=self.net.download,
                       apply_remote=lambda page: applied.append(page))

    def test_upload_batches_and_prunes_only_acked(self):
        for i in range(1, 4):
            self.journal.append_operation(self._op(f"op-{i}", i))
        out = self._job().run_once(generation=7, user_id="u1")
        self.assertTrue(out["ok"])
        self.assertEqual(self.journal.pending_operations(), [])

    def test_stale_generation_and_logged_out_do_no_network(self):
        calls = {"up": 0}

        def _upload(batch):
            calls["up"] += 1
            return {"status": "ok", "acked": []}

        job = SyncJob(generation=7, game_uuid="g", user_id="u1", journal=self.journal,
                      upload=_upload, download=lambda c: (_ for _ in ()).throw(AssertionError("no net")),
                      apply_remote=lambda p: None)
        self.assertIn("stale", job.run_once(generation=8, user_id="u1")["error"])
        self.assertIn("logged_out", job.run_once(generation=7, user_id=None)["error"])
        self.assertEqual(calls["up"], 0)

    def test_permanent_invalid_quarantined_not_retried_forever(self):
        self.journal.append_operation(self._op("op-q", 1))

        def _upload(batch):
            return {"status": "ok", "acked": [], "quarantine": ["op-q"]}

        job = self._job()
        job._upload = _upload
        out = job.run_once(generation=7, user_id="u1")
        self.assertTrue(out["ok"])
        # Quarantined op leaves the outbox but stays for diagnostics.
        self.assertEqual(self.journal.pending_operations(), [])

    def test_single_flight(self):
        job = self._job()
        job.state.running = True
        self.assertIn("already", job.run_once(generation=7, user_id="u1")["error"])

    def test_triggers_fire_on_events_not_timers(self):
        trig = SyncTriggers()
        self.assertTrue(trig.due(manual=True))
        self.assertTrue(trig.due(on_login=True))
        self.assertTrue(trig.due(on_anki_sync=True))
        self.assertTrue(trig.due(new_reviews=200))
        self.assertFalse(trig.due(new_reviews=199))
        self.assertTrue(trig.due(since_last_success_s=20 * 60))
        self.assertFalse(trig.due(since_last_success_s=20 * 60 - 1))
        job = self._job()
        self.assertTrue(job.due(manual=True))
        self.assertFalse(job.due())
        job.note_reviews(200)
        self.assertTrue(job.due())
        out = job.run_once(generation=7, user_id="u1")
        self.assertTrue(out["ok"])
        # Successful sync resets the review counter.
        self.assertFalse(job.due())

    def test_download_before_upload_remote_ingested_locally(self):
        remote_op = dict(self._op("op-remote-1", 1), device_id="dev-b")
        pages = [{"status": "ok",
                  "operations": [remote_op],
                  "next_cursor": "7"}]

        def _download(cursor):
            return dict(pages[0])

        applied = []
        job = SyncJob(generation=7, game_uuid="game-sync-1", user_id="u1",
                      journal=self.journal, upload=self.net.upload,
                      download=_download,
                      apply_remote=lambda page: applied.append(page))
        out = job.run_once(generation=7, user_id="u1")
        self.assertTrue(out["ok"])
        self.assertEqual(len(applied), 1)
        # Remote op persisted locally (merge), cursor saved.
        self.assertIn("op-remote-1", self.journal.operation_ids())
        self.assertEqual(self.journal.get_server_cursor("game-sync-1"), "7")

    def test_remote_id_conflict_quarantined_not_reuploaded(self):
        mine = self._op("op-shared", 1)
        self.journal.append_operation(mine)
        poisoned = dict(self._op("op-shared", 9), device_id="dev-b",
                        payload={"review_key": "rk-CHANGED"})

        def _download(cursor):
            return {"status": "ok", "operations": [poisoned],
                    "next_cursor": "1"}

        seen = {}

        def _upload(batch):
            seen["batch"] = list(batch)
            return {"status": "ok", "acked": [op["op_id"] for op in batch]}

        job = SyncJob(generation=7, game_uuid="game-sync-1", user_id="u1",
                      journal=self.journal, upload=_upload,
                      download=_download, apply_remote=lambda page: None)
        out = job.run_once(generation=7, user_id="u1")
        self.assertTrue(out["ok"])
        # Original payload preserved; poisoned op left the outbox (quarantined).
        self.assertEqual(self.journal.find_operation_payload("op-shared"),
                         {"review_key": "rk-op-shared"})
        self.assertEqual(self.journal.pending_operations(), [])

    def test_wire_projection_never_ships_storage_columns(self):
        op = dict(self._op("op-wire", 3), payload_json='{"review_key": "rk-x"}',
                  acked=0, payload_hash="abc", created_at=123)
        wire = SyncJob._wire_op(op)
        self.assertEqual(set(wire),
                         {"op_id", "device_id", "device_seq",
                          "lamport", "kind", "payload"})
        self.assertEqual(wire["payload"], {"review_key": "rk-op-wire"})
        # String payloads (raw journal rows) parse through.
        wire2 = SyncJob._wire_op(dict(op, payload='{"review_key": "rk-s"}'))
        self.assertEqual(wire2["payload"], {"review_key": "rk-s"})
        # True journal rows carry payload_json with NO payload key; the wire
        # op must carry the real payload, never an empty object.
        row = {"op_id": "op-json", "device_id": "dev-a", "device_seq": 1,
               "lamport": 1, "kind": "review_award",
               "payload_json": '{"review_key": "rk-json", "reward_policy": 2}'}
        wire3 = SyncJob._wire_op(row)
        self.assertEqual(wire3["payload"],
                         {"review_key": "rk-json", "reward_policy": 2})

    def test_response_lost_after_commit_no_double_count(self):
        for i in (1, 2):
            self.journal.append_operation(self._op(f"op-lost-{i}", i))
        calls = {"n": 0}

        def _upload_once(batch):
            calls["n"] += 1
            raise RuntimeError("transient:connection reset")

        job = self._job()
        job._upload = _upload_once
        with self.assertRaises(RuntimeError):
            job.run_once(generation=7, user_id="u1")
        # Nothing acked on transport failure: both ops still pending, retry
        # later cannot inflate (server dedups by op_id, exact-retry = same).
        self.assertEqual(len(self.journal.pending_operations()), 2)
        job2 = self._job()
        out = job2.run_once(generation=7, user_id="u1")
        self.assertTrue(out["ok"])
        self.assertEqual(self.journal.pending_operations(), [])
        self.assertEqual(len(self.net.uploaded[0]), 2)

    def test_backend_pause_backoff_then_resume(self):
        self.journal.append_operation(self._op("op-pause", 1))
        attempts = {"n": 0}

        def _flaky(batch):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return {"status": "rate_limited", "retry_after": 1}
            return {"status": "ok", "acked": [op["op_id"] for op in batch]}

        job = self._job()
        job._upload = _flaky
        with self.assertRaises(RuntimeError):
            job.run_once(generation=7, user_id="u1")
        self.assertGreaterEqual(job.backoff_delay(), 1.0)
        out = job.run_once(generation=7, user_id="u1")
        self.assertTrue(out["ok"])
        self.assertEqual(self.journal.pending_operations(), [])

    def test_new_reviews_during_upload_not_cleared_on_ack(self):
        self.journal.append_operation(self._op("op-old", 1))

        def _upload_and_grow(batch):
            # A new review lands mid-upload; ack covers only the snapshot.
            self.journal.append_operation(self._op("op-new", 2))
            acked = [op["op_id"] for op in batch
                     if op["op_id"] != "op-new"]
            return {"status": "ok", "acked": acked}

        job = self._job()
        job._upload = _upload_and_grow
        out = job.run_once(generation=7, user_id="u1")
        self.assertTrue(out["ok"])
        pending = [op["op_id"] for op in self.journal.pending_operations()]
        self.assertEqual(pending, ["op-new"])


if __name__ == "__main__":
    unittest.main()
