import os
import tempfile
import unittest

from evolved.journal import Journal
from evolved.sync import SyncJob


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


if __name__ == "__main__":
    unittest.main()
