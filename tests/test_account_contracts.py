# tests/test_account_contracts.py - Production-wiring regressions.
"""These tests exercise the real ProfileSession, the real make_transport
wiring, the real post_json transport and a loopback HTTP fixture. The earlier
suites mocked post_json and used MemorySession directly, which is exactly how
the two user-visible failures (missing access_token forwarding and the
object-only JSON check rejecting the Hiscores array) escaped detection.
"""
from __future__ import annotations

import http.server
import json
import threading
import time
import unittest

from evolved.net import Endpoint, NetError, post_json
from evolved.service import (ServiceConfig, make_transport, query_hiscores,
                             query_public_profile)
from evolved.session_store import ProfileSession

CAPS_PATH = "/rest/v1/rpc/evolved_capabilities"
SUBMIT_PATH = "/rest/v1/rpc/submit_operations"
HISCORES_PATH = "/rest/v1/rpc/hiscores"
PROFILE_PATH = "/rest/v1/rpc/public_profile"


class _Handler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}
    seen: list = []

    def log_message(self, *args):  # keep unittest output clean
        pass

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = {"__unparsed__": raw.decode("utf-8", "replace")}
        type(self).seen.append({"path": self.path,
                                "auth": self.headers.get("Authorization") or "",
                                "body": body})
        responses = type(self).routes.get(self.path)
        if not responses:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        entry = responses.pop(0) if len(responses) > 1 else responses[0]
        status, payload = entry
        data = (payload if isinstance(payload, bytes)
                else json.dumps(payload).encode("utf-8"))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _Fixture:
    """Loopback HTTP server the real post_json talks to."""

    def __enter__(self):
        handler = type("_BoundHandler", (_Handler,),
                       {"routes": {}, "seen": []})
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
        self._handler = handler
        self._thread = threading.Thread(target=self.httpd.serve_forever,
                                        daemon=True)
        self._thread.start()
        self.endpoint = Endpoint(
            base_url=f"http://127.0.0.1:{self.httpd.server_port}",
            project_key="test-anon", allow_http_loopback=True)
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=2)

    def route(self, path, *responses):
        self._handler.routes[path] = list(responses)

    @property
    def seen(self):
        return self._handler.seen


def _profile_session(*, token="tok-old", user_id="u-1", username="wilson"):
    sess = ProfileSession(generation=1)
    sess.session.set(access_token=token, refresh_token="refresh-1",
                     user_id=user_id, username=username)
    return sess


class TestProfileSessionToken(unittest.TestCase):
    def test_access_token_is_exposed_and_delegated(self):
        sess = ProfileSession(generation=1)
        self.assertIsNone(sess.access_token)
        sess.session.set(access_token="a-1", refresh_token="r-1",
                         user_id="u-1", username="wilson")
        self.assertEqual(sess.access_token, "a-1")
        sess.clear()
        self.assertIsNone(sess.access_token)

    def test_remembered_session_restores_token_through_property(self):
        class _FakeVault:
            available = True

            def __init__(self):
                self.saved = None

            def read(self):
                return {"access_token": "vault-a", "refresh_token": "vault-r",
                        "user_id": "u-9", "username": "wilson"}

            def write(self, payload):
                self.saved = dict(payload)
                return True

            def delete(self):
                return True

        sess = ProfileSession(generation=2, vault=_FakeVault())
        self.assertEqual(sess.access_token, "vault-a")
        self.assertTrue(sess.logged_in)

    def test_make_transport_uses_profile_session_token(self):
        with _Fixture() as fx:
            fx.route(CAPS_PATH, (200, {"authoritative_scoring": True}))
            fx.route(SUBMIT_PATH, (200, {"accepted": ["op-1"],
                                         "conflicts": []}))
            sess = _profile_session(token="tok-live")
            transport = make_transport(ServiceConfig(
                endpoint=fx.endpoint, game_uuid="game-1", post=post_json), sess)
            out = transport["upload"]([{
                "op_id": "op-1", "device_id": "dev-a", "device_seq": 1,
                "lamport": 1, "kind": "review_award", "payload": {}}])
            self.assertEqual(out["status"], "ok")
            self.assertEqual(out["acked"], ["op-1"])
            submits = [entry for entry in fx.seen
                       if entry["path"] == SUBMIT_PATH]
            self.assertEqual(submits[0]["auth"], "Bearer tok-live")

    def test_401_refreshes_once_and_retries_with_rotated_token(self):
        with _Fixture() as fx:
            fx.route(CAPS_PATH, (200, {"authoritative_scoring": True}))
            fx.route(SUBMIT_PATH, (401, {"message": "jwt expired"}),
                     (200, {"accepted": ["op-1"], "conflicts": []}))
            sess = _profile_session(token="tok-old")

            def _refresh(memory):
                memory.set(access_token="tok-new", refresh_token="refresh-2",
                           user_id="u-1", username="wilson")
                return True

            sess.bind_refresh(_refresh)
            transport = make_transport(ServiceConfig(
                endpoint=fx.endpoint, game_uuid="game-1", post=post_json), sess)
            out = transport["upload"]([{
                "op_id": "op-1", "device_id": "dev-a", "device_seq": 1,
                "lamport": 1, "kind": "review_award", "payload": {}}])
            self.assertEqual(out["status"], "ok")
            auths = [entry["auth"] for entry in fx.seen
                     if entry["path"] == SUBMIT_PATH]
            self.assertEqual(auths, ["Bearer tok-old", "Bearer tok-new"])
            self.assertEqual(sess.access_token, "tok-new")


class TestPostJsonShapes(unittest.TestCase):
    def test_object_mode_is_default_and_rejects_array(self):
        with _Fixture() as fx:
            fx.route(HISCORES_PATH, (200, [{"rank": 1}]))
            with self.assertRaises(NetError) as ctx:
                post_json(fx.endpoint, HISCORES_PATH, {})
            self.assertEqual(ctx.exception.kind, "malformed_response")

    def test_array_mode_returns_list(self):
        with _Fixture() as fx:
            fx.route(HISCORES_PATH, (200, [{"rank": 1, "username": "Amy"}]))
            data = post_json(fx.endpoint, HISCORES_PATH, {},
                             response_shape="array")
            self.assertIsInstance(data, list)
            self.assertEqual(data[0]["username"], "Amy")

    def test_array_mode_rejects_object(self):
        with _Fixture() as fx:
            fx.route(PROFILE_PATH, (200, {"not": "a list"}))
            with self.assertRaises(NetError) as ctx:
                post_json(fx.endpoint, PROFILE_PATH, {},
                          response_shape="array")
            self.assertEqual(ctx.exception.kind, "malformed_response")

    def test_both_modes_reject_null_and_scalars(self):
        with _Fixture() as fx:
            for payload in (None, 7, "text", True):
                fx.route(HISCORES_PATH, (200, payload))
                with self.assertRaises(NetError):
                    post_json(fx.endpoint, HISCORES_PATH, {},
                              response_shape="array")
                with self.assertRaises(NetError):
                    post_json(fx.endpoint, HISCORES_PATH, {})

    def test_unsupported_shape_rejected_before_http(self):
        with _Fixture() as fx:
            fx.route(HISCORES_PATH, (200, []))
            with self.assertRaises(NetError) as ctx:
                post_json(fx.endpoint, HISCORES_PATH, {},
                          response_shape="tuple")
            self.assertEqual(ctx.exception.kind, "invalid")
            self.assertEqual(fx.seen, [])


class TestQueryHiscoresRealHttp(unittest.TestCase):
    def test_real_http_array_produces_ranked_rows(self):
        with _Fixture() as fx:
            fx.route(HISCORES_PATH, (200, [
                {"rank": 1, "username": "Amy", "xp": 5_000_000},
                {"rank": 2, "username": "Wilson", "xp": 2_500_000},
            ]))
            rows = query_hiscores(post_json, fx.endpoint, None,
                                  skill="mining", limit=50)
            self.assertEqual(rows[0]["rank"], 1)
            self.assertEqual(rows[0]["xp_display"], "5")
            self.assertEqual(rows[1]["username"], "Wilson")
            self.assertEqual(fx.seen[0]["path"], HISCORES_PATH)
            self.assertEqual(fx.seen[0]["body"]["p_skill"], "mining")

    def test_empty_array_is_successful_empty_state(self):
        with _Fixture() as fx:
            fx.route(HISCORES_PATH, (200, []))
            self.assertEqual(query_hiscores(post_json, fx.endpoint, None,
                                            skill="mining"), [])

    def test_malformed_rows_raise_typed_error_not_attribute_errors(self):
        bad_rows = (
            {"rank": 1, "username": "", "xp": 1},
            {"rank": 1, "username": "Amy", "xp": -5},
            {"rank": 1, "username": "Amy", "xp": "not-a-number"},
            {"rank": 0, "username": "Amy", "xp": 5},
            {"rank": None, "username": "Amy", "xp": 5},
            {"username": "Amy", "xp": 5},
            "not-a-row",
        )
        with _Fixture() as fx:
            for row in bad_rows:
                fx.route(HISCORES_PATH, (200, [row]))
                with self.assertRaises(NetError) as ctx:
                    query_hiscores(post_json, fx.endpoint, None,
                                   skill="mining")
                self.assertEqual(ctx.exception.kind, "malformed_response",
                                 msg=f"row {row!r}")


class TestQueryPublicProfile(unittest.TestCase):
    def _route_found(self, fx, *, state_xp, hiscores_rows):
        fx.route(PROFILE_PATH, (200, {"username": "Amy",
                                      "state": {"xp": state_xp}}))
        fx.route(HISCORES_PATH, (200, hiscores_rows))

    def test_found_user_inside_top_list_gets_rank(self):
        with _Fixture() as fx:
            self._route_found(
                fx, state_xp={"mining": 5_000_000},
                hiscores_rows=[{"rank": 3, "username": "Amy",
                                "xp": 5_000_000}])
            out = query_public_profile(post_json, fx.endpoint, None,
                                       username="Amy", skill="mining",
                                       limit=50)
            self.assertTrue(out["ok"])
            self.assertEqual(out["profile"]["username"], "Amy")
            self.assertEqual(out["profile"]["rank"], 3)
            self.assertEqual(out["rows"][0]["rank"], 3)
            self.assertEqual(out["rows"][0]["xp_display"], "5")

    def test_found_user_outside_top_list_rank_unavailable(self):
        with _Fixture() as fx:
            self._route_found(
                fx, state_xp={"mining": 123_000},
                hiscores_rows=[{"rank": 1, "username": "Someone",
                                "xp": 9_000_000}])
            out = query_public_profile(post_json, fx.endpoint, None,
                                       username="Amy", skill="mining",
                                       limit=50)
            self.assertTrue(out["ok"])
            self.assertIsNone(out["profile"]["rank"])
            self.assertIsNone(out["rows"][0]["rank"])
            self.assertEqual(out["rows"][0]["xp_display"], "0.123")

    def test_mixed_case_input_is_normalized_for_the_rpc(self):
        with _Fixture() as fx:
            self._route_found(
                fx, state_xp={"mining": 1_000_000},
                hiscores_rows=[{"rank": 1, "username": "Amy",
                                "xp": 1_000_000}])
            out = query_public_profile(post_json, fx.endpoint, None,
                                       username="  aMy ", skill="mining")
            self.assertTrue(out["ok"])
            self.assertEqual(fx.seen[0]["body"]["p_username_norm"], "amy")

    def test_nonexistent_user_is_friendly_not_found(self):
        with _Fixture() as fx:
            fx.route(PROFILE_PATH, (404, {"code": "02000",
                                          "message": "no_profile"}))
            out = query_public_profile(post_json, fx.endpoint, None,
                                       username="ghost", skill="mining")
            self.assertFalse(out["ok"])
            self.assertTrue(out["not_found"])
            self.assertNotIn("error", out)

    def test_malformed_profile_response_raises(self):
        with _Fixture() as fx:
            fx.route(PROFILE_PATH, (200, [{"not": "an object"}]))
            with self.assertRaises(NetError):
                query_public_profile(post_json, fx.endpoint, None,
                                     username="Amy", skill="mining")

    def test_backend_unavailable_raises_connection_error(self):
        endpoint = Endpoint(base_url="http://127.0.0.1:1", project_key="k",
                            allow_http_loopback=True)
        with self.assertRaises(NetError) as ctx:
            query_public_profile(post_json, endpoint, None,
                                 username="Amy", skill="mining")
        self.assertIn(ctx.exception.kind, ("connection", "server", "http"))

    def test_invalid_username_is_rejected_locally(self):
        with _Fixture() as fx:
            out = query_public_profile(post_json, fx.endpoint, None,
                                       username="bad name!", skill="mining")
            self.assertFalse(out["ok"])
            self.assertEqual(fx.seen, [])


class TestJournalOnAuthFailure(unittest.TestCase):
    def test_failed_refresh_keeps_pending_and_reports_auth_state(self):
        import os
        import tempfile

        from evolved.journal import Journal
        from evolved.service import SyncService

        with _Fixture() as fx:
            fx.route(CAPS_PATH, (200, {"authoritative_scoring": True}))
            fx.route("/rest/v1/rpc/fetch_operations",
                     (200, {"operations": [], "next_cursor": 0}))
            fx.route(SUBMIT_PATH, (401, {"message": "jwt expired"}))
            sess = _profile_session(token="tok-expired")  # refresh fails closed
            with tempfile.TemporaryDirectory() as tmp:
                journal = Journal(os.path.join(tmp, "game.sqlite3"))
                try:
                    journal.append_operation({
                        "op_id": "op-keep", "game_uuid": "game-1",
                        "device_id": "dev-a", "device_seq": 1, "lamport": 1,
                        "kind": "review_award",
                        "payload": {"review_key": "rk-keep"}})
                    svc = SyncService(
                        generation=1, game_uuid="game-1", journal=journal,
                        transport=make_transport(ServiceConfig(
                            endpoint=fx.endpoint, game_uuid="game-1",
                            post=post_json), sess),
                        apply_remote=lambda page: None,
                        get_user_id=lambda: "u-1")
                    out = svc.force_sync()
                    self.assertFalse(out["ok"])
                    self.assertIn("unauthorized", out["error"])
                    self.assertEqual(len(journal.pending_operations()), 1)
                finally:
                    journal.close()


class TestMalformedEndpointPayloads(unittest.TestCase):
    def test_invalid_json_is_malformed_response(self):
        with _Fixture() as fx:
            fx.route(HISCORES_PATH, (200, b"{not json"))
            with self.assertRaises(NetError) as ctx:
                post_json(fx.endpoint, HISCORES_PATH, {})
            self.assertEqual(ctx.exception.kind, "malformed_response")
            with self.assertRaises(NetError) as ctx2:
                post_json(fx.endpoint, HISCORES_PATH, {},
                          response_shape="array")
            self.assertEqual(ctx2.exception.kind, "malformed_response")


if __name__ == "__main__":
    unittest.main()
