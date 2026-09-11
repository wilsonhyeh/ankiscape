import os
import tempfile
import unittest

from evolved.journal import Journal
from evolved.net import Endpoint, NetError
from evolved.service import ServiceConfig, SyncService, _map_net_error, make_transport


class _Session:
    def __init__(self, token="tok", user_id="u1"):
        self.access_token = token
        self.user_id = user_id
        self.refresh_calls = 0
        self._fail_refresh = False

    def refresh(self):
        self.refresh_calls += 1
        if self._fail_refresh:
            return False
        self.access_token = "tok-new"
        return True


def _op(op_id, seq, **kw):
    base = {"op_id": op_id, "game_uuid": "game-svc-1", "device_id": "dev-a",
            "device_seq": seq, "lamport": seq, "kind": "review_award",
            "payload": {"review_key": f"rk-{op_id}"}}
    base.update(kw)
    return base


class TestMapNetError(unittest.TestCase):
    def test_rate_limited_carries_retry_after(self):
        out = _map_net_error(NetError("rate_limited", "slow", retry_after=7))
        self.assertEqual(out["status"], "rate_limited")
        self.assertEqual(out["retry_after"], 7)

    def test_permanent_kinds(self):
        for kind, want in (("unauthorized", "unauthorized"),
                           ("forbidden", "forbidden"),
                           ("invalid", "invalid"),
                           ("conflict", "conflict")):
            self.assertEqual(_map_net_error(NetError(kind, "d"))["status"], want)

    def test_transient_bucket(self):
        self.assertEqual(_map_net_error(NetError("connection", "down"))["status"],
                         "transient")
        self.assertEqual(_map_net_error(NetError("server", "500"))["status"],
                         "transient")


class TestTransport(unittest.TestCase):
    def setUp(self):
        self.session = _Session()
        self.calls = []
        self.endpoint = Endpoint(base_url="https://api.example.com", project_key="pub")

    def _post(self, endpoint, path, payload, access_token=None):
        self.calls.append((path, payload, access_token))
        if path.endswith("evolved_capabilities"):
            return {"protocol_version": 2, "authoritative_scoring": True}
        if path.endswith("submit_operations"):
            return {"accepted": [op["op_id"] for op in payload["p_ops"]],
                    "conflicts": []}
        if path.endswith("fetch_operations"):
            return {"operations": [], "next_cursor": 0, "revision": 1}
        raise AssertionError(path)

    def test_upload_shape_and_auth_header(self):
        t = make_transport(ServiceConfig(endpoint=self.endpoint,
                                         game_uuid="game-svc-1", post=self._post),
                           self.session)
        out = t["upload"]([_op("op-1", 1)])
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["acked"], ["op-1"])
        submit_calls = [c for c in self.calls
                        if c[0].endswith("submit_operations")]
        self.assertEqual(len(submit_calls), 1)
        path, payload, token = submit_calls[0]
        self.assertEqual(token, "tok")
        self.assertEqual(set(payload["p_ops"][0]),
                         {"op_id", "device_id", "device_seq",
                          "lamport", "kind", "payload"})

    def test_old_server_requires_update_and_keeps_pending(self):
        def _post(endpoint, path, payload, access_token=None):
            raise NetError("not_found", "no such function")

        t = make_transport(ServiceConfig(endpoint=self.endpoint,
                                         game_uuid="game-svc-1", post=_post),
                           self.session)
        out = t["upload"]([_op("op-old", 1)])
        self.assertEqual(out["status"], "upgrade_required")

    def test_401_refreshes_once_then_retries(self):
        attempts = {"submit": 0}

        def _post(endpoint, path, payload, access_token=None):
            if path.endswith("evolved_capabilities"):
                return {"authoritative_scoring": True}
            attempts["submit"] += 1
            if attempts["submit"] == 1:
                raise NetError("unauthorized", "expired")
            return {"accepted": [op["op_id"] for op in payload["p_ops"]],
                    "conflicts": []}

        t = make_transport(ServiceConfig(endpoint=self.endpoint,
                                         game_uuid="game-svc-1", post=_post),
                           self.session)
        out = t["upload"]([_op("op-2", 1)])
        self.assertEqual(out["status"], "ok")
        self.assertEqual(self.session.refresh_calls, 1)
        self.assertEqual(attempts["submit"], 2)

    def test_401_refresh_failure_maps_permanent(self):
        self.session._fail_refresh = True

        def _post(endpoint, path, payload, access_token=None):
            if path.endswith("evolved_capabilities"):
                return {"authoritative_scoring": True}
            raise NetError("unauthorized", "expired")

        t = make_transport(ServiceConfig(endpoint=self.endpoint,
                                         game_uuid="game-svc-1", post=_post),
                           self.session)
        out = t["upload"]([_op("op-3", 1)])
        self.assertEqual(out["status"], "unauthorized")
        self.assertEqual(self.session.refresh_calls, 1)

    def test_429_maps_rate_limited(self):
        def _post(endpoint, path, payload, access_token=None):
            if path.endswith("evolved_capabilities"):
                return {"authoritative_scoring": True}
            raise NetError("rate_limited", "slow", retry_after=4)

        t = make_transport(ServiceConfig(endpoint=self.endpoint,
                                         game_uuid="game-svc-1", post=_post),
                           self.session)
        out = t["upload"]([_op("op-4", 1)])
        self.assertEqual(out["status"], "rate_limited")
        self.assertEqual(out["retry_after"], 4)

    def test_malformed_response_is_transient(self):
        t = make_transport(ServiceConfig(endpoint=self.endpoint,
                                         game_uuid="game-svc-1",
                                         post=lambda *a, **k: [1, 2, 3]),
                           self.session)
        out = t["upload"]([_op("op-5", 1)])
        self.assertEqual(out["status"], "transient")


class TestSyncService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(os.path.join(self.tmp.name, "game.sqlite3"))
        self.addCleanup(self.journal.close)
        self.user_id = "u1"
        self.uploaded = []
        self.pages = [{"status": "ok", "operations": [], "next_cursor": "0"}]

    def _svc(self):
        def _upload(batch):
            self.uploaded.append(list(batch))
            return {"status": "ok", "acked": [op["op_id"] for op in batch]}

        def _download(cursor):
            return dict(self.pages[0])

        return SyncService(
            generation=3, game_uuid="game-svc-1", journal=self.journal,
            transport={"upload": _upload, "download": _download},
            apply_remote=lambda page: None,
            get_user_id=lambda: self.user_id)

    def test_logged_out_does_no_network(self):
        self.user_id = None
        svc = self._svc()
        out = svc.maybe_sync(manual=True)
        self.assertIn("logged_out", out["error"])
        self.assertEqual(self.uploaded, [])

    def test_offline_reviews_queue_then_upload_on_login(self):
        # Offline: local credit persists with no network at all.
        self.journal.append_operation(_op("op-off-1", 1))
        self.user_id = None
        svc = self._svc()
        out = svc.maybe_sync(manual=True)
        self.assertFalse(out["ok"])
        self.assertEqual(len(self.journal.pending_operations()), 1)
        # Login: manual sync uploads the queued op exactly once.
        self.user_id = "u1"
        out = svc.maybe_sync(manual=True)
        self.assertTrue(out["ok"])
        self.assertEqual(self.journal.pending_operations(), [])
        self.assertEqual(len(self.uploaded[0]), 1)

    def test_two_clients_converge(self):
        # Client B's op arrives via download; both end up stored locally.
        mine = _op("op-a-1", 1)
        self.journal.append_operation(mine)
        theirs = _op("op-b-1", 1, device_id="dev-b")
        self.pages[0] = {"status": "ok", "operations": [theirs],
                         "next_cursor": "5"}
        svc = self._svc()
        out = svc.maybe_sync(manual=True)
        self.assertTrue(out["ok"])
        ids = sorted(self.journal.operation_ids())
        self.assertEqual(ids, ["op-a-1", "op-b-1"])
        # Download-first merge, then upload: the merged remote op is still
        # unacknowledged locally, so it uploads too (server dedups by op_id;
        # exact-retry returns the same acceptance). Convergence = both stored,
        # both acked, nothing pending.
        self.assertEqual(sorted(op["op_id"] for op in self.uploaded[0]),
                         ["op-a-1", "op-b-1"])
        self.assertEqual(self.journal.pending_operations(), [])

    def test_rate_limited_surfaces_backoff_pending_kept(self):
        self.journal.append_operation(_op("op-rl", 1))

        def _limited(batch):
            return {"status": "rate_limited", "retry_after": 9}

        svc = self._svc()
        svc.transport = dict(svc.transport, upload=_limited)
        svc.job._upload = _limited
        out = svc.maybe_sync(manual=True)
        self.assertFalse(out["ok"])
        self.assertIn("rate_limited", out["error"])
        self.assertEqual(len(self.journal.pending_operations()), 1)
        self.assertGreaterEqual(svc.backoff_hint_s(), 1.0)

    def test_refresh_serialized(self):
        svc = self._svc()
        svc._refresh_lock.acquire()
        try:
            self.assertFalse(svc.try_refresh(lambda: True))
        finally:
            svc._refresh_lock.release()
        self.assertTrue(svc.try_refresh(lambda: True))

    def test_user_switch_rebinds(self):
        svc = self._svc()
        self.assertTrue(svc.maybe_sync(manual=True)["ok"])
        self.user_id = "u2"
        out = svc.maybe_sync(manual=True)
        self.assertTrue(out["ok"])
        self.assertEqual(svc.job.user_id, "u2")

    def test_status_line_render_shape(self):
        self.journal.append_operation(_op("op-st", 1))
        svc = self._svc()
        line = svc.status_line()
        self.assertTrue(line["logged_in"])
        self.assertEqual(line["pending"], 1)
        self.assertIn("last_error", line)


if __name__ == "__main__":
    unittest.main()
