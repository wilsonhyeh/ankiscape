import os
import tempfile
import unittest

from evolved.journal import Journal, JournalProtocolError
from evolved.sync import SyncJob, TURN_MAX_S


class _Net:
    def __init__(self):
        self.uploaded = []
        self.pages = []
        self.upload_result = None

    def upload(self, batch):
        self.uploaded.append([op["op_id"] for op in batch])
        if self.upload_result is not None:
            return self.upload_result
        return {"status": "ok", "acked": [op["op_id"] for op in batch]}

    def download(self, cursor):
        if self.pages:
            return self.pages.pop(0)
        return {"status": "ok", "operations": [], "next_cursor": cursor}


class TestJournalIngest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(os.path.join(self.tmp.name, "game.sqlite3"))
        self.addCleanup(self.journal.close)

    def _remote(self, op_id, seq=1, payload=None, game="g1"):
        return {"op_id": op_id, "game_uuid": game, "device_id": "dev-b",
                "device_seq": seq, "lamport": seq, "kind": "review_award",
                "payload": payload or {"review_key": f"rk-{op_id}"}}

    def test_page_and_cursor_commit_together_and_not_to_outbox(self):
        page = {"operations": [self._remote("r1", 1)],
                "next_cursor": "11", "has_more": False}
        out = self.journal.ingest_remote_page("g1", page)
        self.assertEqual(out["stored"], 1)
        self.assertEqual(self.journal.get_server_cursor("g1"), "11")
        self.assertEqual(self.journal.pending_operations(), [])
        self.assertEqual(self.journal.operation_ids(), ["r1"])
        # Remote rows are acknowledged locally (never uploaded back).
        self.assertEqual(self.journal.count_pending_operations(), 0)

    def test_malformed_row_commits_nothing_and_keeps_cursor(self):
        page = {"operations": [self._remote("r1", 1),
                               {"op_id": "", "kind": "review_award",
                                "payload": {}}],
                "next_cursor": "11", "has_more": True}
        with self.assertRaises(JournalProtocolError) as ctx:
            self.journal.ingest_remote_page("g1", page)
        self.assertEqual(ctx.exception.code, "malformed_row")
        self.assertEqual(self.journal.operation_ids(), [])
        self.assertEqual(self.journal.get_server_cursor("g1"), "")

    def test_id_conflict_is_typed_and_commits_nothing(self):
        self.journal.append_operation(
            {"op_id": "r1", "game_uuid": "g1", "device_id": "dev-a",
             "device_seq": 1, "lamport": 1, "kind": "review_award",
             "payload": {"review_key": "rk-original"}})
        page = {"operations": [self._remote("r1", 9,
                                            {"review_key": "rk-CHANGED"})],
                "next_cursor": "3", "has_more": False}
        with self.assertRaises(JournalProtocolError) as ctx:
            self.journal.ingest_remote_page("g1", page)
        self.assertEqual(ctx.exception.code, "id_conflict")
        self.assertEqual(self.journal.find_operation_payload("r1"),
                         {"review_key": "rk-original"})
        self.assertEqual(self.journal.get_server_cursor("g1"), "")

    def test_exact_duplicates_are_idempotent(self):
        page = {"operations": [self._remote("r1", 1)],
                "next_cursor": "5", "has_more": False}
        self.journal.ingest_remote_page("g1", page)
        second = self.journal.ingest_remote_page("g1", page)
        self.assertEqual(second["stored"], 0)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(self.journal.operation_count(), 1)

    def test_cursor_not_advanced_with_more_is_protocol_error(self):
        self.journal.ingest_remote_page(
            "g1", {"operations": [], "next_cursor": "9", "has_more": False})
        page = {"operations": [self._remote("r2", 2)],
                "next_cursor": "9", "has_more": True}
        with self.assertRaises(JournalProtocolError) as ctx:
            self.journal.ingest_remote_page("g1", page)
        self.assertEqual(ctx.exception.code, "cursor_not_advanced")
        # The row from the bad page was not committed either.
        self.assertEqual(self.journal.operation_ids(), [])

    def test_foreign_game_rows_are_protocol_error(self):
        page = {"operations": [self._remote("r1", 1, game="other")],
                "next_cursor": "3", "has_more": False}
        with self.assertRaises(JournalProtocolError) as ctx:
            self.journal.ingest_remote_page("g1", page)
        self.assertEqual(ctx.exception.code, "foreign_game")

    def test_cursor_regression_is_protocol_error(self):
        self.journal.ingest_remote_page(
            "g1", {"operations": [], "next_cursor": "9", "has_more": False})
        page = {"operations": [], "next_cursor": "4", "has_more": False}
        with self.assertRaises(JournalProtocolError) as ctx:
            self.journal.ingest_remote_page("g1", page)
        self.assertEqual(ctx.exception.code, "cursor_regressed")


class TestSyncJobDurable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(os.path.join(self.tmp.name, "game.sqlite3"))
        self.addCleanup(self.journal.close)
        self.net = _Net()

    def _op(self, op_id, seq):
        return {"op_id": op_id, "game_uuid": "g1", "device_id": "dev-a",
                "device_seq": seq, "lamport": seq, "kind": "review_award",
                "payload": {"review_key": f"rk-{op_id}"}}

    def _job(self):
        return SyncJob(generation=1, game_uuid="g1", user_id="u1",
                       journal=self.journal, upload=self.net.upload,
                       download=self.net.download,
                       apply_remote=lambda page: None)

    def test_missing_ack_list_is_protocol_error(self):
        self.journal.append_operation(self._op("op-1", 1))
        self.net.upload_result = {"status": "ok"}  # no acked key
        with self.assertRaises(RuntimeError) as ctx:
            self._job().run_once(generation=1, user_id="u1")
        self.assertIn("protocol_error", str(ctx.exception))
        self.assertEqual(len(self.journal.pending_operations()), 1)
        self.assertEqual(self.job_error_kind(), "protocol_error")

    def job_error_kind(self):
        return self._job()._error_kind(RuntimeError("protocol_error:x"))

    def test_full_batch_with_no_ack_no_quarantine_is_no_progress(self):
        from evolved.sync import UPLOAD_BATCH_MAX
        for i in range(UPLOAD_BATCH_MAX):
            self.journal.append_operation(self._op(f"op-{i}", i + 1))
        self.net.upload_result = {"status": "ok", "acked": []}
        with self.assertRaises(RuntimeError) as ctx:
            self._job().run_once(generation=1, user_id="u1")
        self.assertIn("no_progress", str(ctx.exception))
        self.assertEqual(len(self.journal.pending_operations()),
                         UPLOAD_BATCH_MAX)

    def test_quarantined_ids_count_as_rejected_and_leave_outbox(self):
        self.journal.append_operation(self._op("op-bad", 1))
        self.net.upload_result = {"status": "ok", "acked": [],
                                  "quarantine": ["op-bad"]}
        out = self._job().run_once(generation=1, user_id="u1")
        self.assertTrue(out["ok"])
        self.assertEqual(out["rejected"], 1)
        self.assertEqual(self.journal.count_rejected_operations(), 1)
        self.assertEqual(self.journal.pending_operations(), [])

    def test_turn_bound_returns_continue_without_blocking(self):
        # A zero processing budget forces a continuation after one page.
        import evolved.sync as sync_mod
        original = sync_mod.TURN_MAX_S
        sync_mod.TURN_MAX_S = 0.0
        self.addCleanup(setattr, sync_mod, "TURN_MAX_S", original)
        self.net.pages = [
            {"status": "ok",
             "operations": [self._remote("r1", 1), self._remote("r2", 2)],
             "next_cursor": "2", "has_more": True},
        ]
        out = self._job().run_once(generation=1, user_id="u1")
        self.assertTrue(out["ok"])
        self.assertTrue(out["continue"])
        self.assertEqual(out["phase"], "download")
        # The page was durably committed before yielding.
        self.assertEqual(sorted(self.journal.operation_ids()), ["r1", "r2"])

    def _remote(self, op_id, seq):
        return {"op_id": op_id, "game_uuid": "g1", "device_id": "dev-b",
                "device_seq": seq, "lamport": seq, "kind": "review_award",
                "payload": {"review_key": f"rk-{op_id}"}}

    def test_account_switch_between_pages_aborts_before_commit(self):
        calls = {"n": 0}
        original = self._job()

        def _download(cursor):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"status": "ok",
                        "operations": [self._remote("r1", 1)],
                        "next_cursor": "1", "has_more": True}
            # Identity changed during the download: the second page must not
            # be committed for the old identity.
            original.user_id = "u2"
            return {"status": "ok",
                    "operations": [self._remote("r2", 2)],
                    "next_cursor": "2", "has_more": False}

        original._download = _download
        out = original.run_once(generation=1, user_id="u1")
        self.assertFalse(out["ok"])
        self.assertIn("stale", out["error"])
        self.assertEqual(self.journal.operation_ids(), ["r1"])


if __name__ == "__main__":
    unittest.main()
