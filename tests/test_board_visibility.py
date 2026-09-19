# tests/test_board_visibility.py - S1: capability/self_context plumbing.
"""S1: capabilities and self_context are fetched eagerly and cached with an
explicit tri-state; the Settings board-visibility checkbox renders ONLY on
known-true, reads its value from self_context, and persists only through the
named shell handler. Unknown and known-false both hide the control and never
guess a value.
"""
from __future__ import annotations

import http.server
import json
import threading
import unittest

from evolved.net import Endpoint, post_json
from evolved.service import (ServiceConfig, fetch_capabilities,
                             make_transport, set_board_visibility)
from evolved.ui.settings import board_visibility_row, board_visibility_toggled

CAPS_PATH = "/rest/v1/rpc/evolved_capabilities"
SELF_PATH = "/rest/v1/rpc/self_context"


class _Handler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}
    seen: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = {}
        type(self).seen.append({"path": self.path, "body": body})
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


def _session(*, token="tok", user_id="u-1"):
    class _Sess:
        def __init__(self):
            self.access_token = token
            self.user_id = user_id
            self.logged_in = True
    return _Sess()


class TestCapabilityTriState(unittest.TestCase):
    def test_known_true_and_known_false_are_distinguishable(self):
        with _Fixture() as fx:
            fx.route(CAPS_PATH, (200, {"authoritative_scoring": True,
                                       "board_visibility": True}))
            state = fetch_capabilities(post_json, fx.endpoint, _session())
            self.assertEqual(state["board_visibility"],
                             {"state": "known", "value": True})
            self.assertEqual(state["authoritative_scoring"],
                             {"state": "known", "value": True})
        with _Fixture() as fx:
            fx.route(CAPS_PATH, (200, {"authoritative_scoring": True,
                                       "board_visibility": False}))
            state = fetch_capabilities(post_json, fx.endpoint, _session())
            self.assertEqual(state["board_visibility"],
                             {"state": "known", "value": False})

    def test_404_is_unknown_for_an_older_server(self):
        with _Fixture() as fx:
            state = fetch_capabilities(post_json, fx.endpoint, _session())
            self.assertEqual(state["board_visibility"], {"state": "unknown"})

    def test_malformed_reply_is_unknown(self):
        with _Fixture() as fx:
            fx.route(CAPS_PATH, (200, b"{not json"))
            state = fetch_capabilities(post_json, fx.endpoint, _session())
            self.assertEqual(state["board_visibility"], {"state": "unknown"})

    def test_network_failure_is_unknown_and_never_raises(self):
        endpoint = Endpoint(base_url="http://127.0.0.1:1", project_key="k",
                            allow_http_loopback=True)
        state = fetch_capabilities(post_json, endpoint, _session())
        self.assertEqual(state["board_visibility"], {"state": "unknown"})


class TestEagerTransportFetch(unittest.TestCase):
    def test_make_transport_fetches_capabilities_eagerly(self):
        with _Fixture() as fx:
            fx.route(CAPS_PATH, (200, {"board_visibility": True,
                                       "authoritative_scoring": True}))
            transport = make_transport(ServiceConfig(
                endpoint=fx.endpoint, game_uuid="game-1", post=post_json),
                _session())
            paths = [entry["path"] for entry in fx.seen]
            self.assertIn(CAPS_PATH, paths,
                          "capabilities must be fetched at construction")
            self.assertEqual(transport["capabilities"]["board_visibility"],
                             {"state": "known", "value": True})

    def test_transport_survives_an_offline_construction(self):
        endpoint = Endpoint(base_url="http://127.0.0.1:1", project_key="k",
                            allow_http_loopback=True)
        transport = make_transport(ServiceConfig(
            endpoint=endpoint, game_uuid="game-1", post=post_json),
            _session())
        self.assertEqual(transport["capabilities"]["board_visibility"],
                         {"state": "unknown"})

    def test_fetch_self_context_is_the_eager_product_read(self):
        # The product service construction fetches self_context alongside the
        # transport's eager capabilities (S1).
        from evolved.service import fetch_self_context
        with _Fixture() as fx:
            fx.route(SELF_PATH, (200, {"username": "acct", "is_test": False,
                                       "visible_on_board": True}))
            context = fetch_self_context(post_json, fx.endpoint, _session())
            self.assertEqual(context["visible_on_board"], True)
            self.assertEqual(fx.seen[0]["path"], SELF_PATH)


class _Shell:
    """Minimal shell.call surface (no Qt)."""

    def __init__(self, deps):
        self._deps = dict(deps)
        self.calls = []

    def call(self, name, *args, default=None):
        self.calls.append((name, args))
        fn = self._deps.get(name)
        if not callable(fn):
            return default
        return fn(*args)


def _caps(state, value=None):
    entry = {"state": state}
    if state == "known":
        entry["value"] = value
    return {"board_visibility": entry}


class TestBoardVisibilityGate(unittest.TestCase):
    def _shell(self, caps, ctx):
        return _Shell({"get_evolved_capabilities": lambda: caps,
                       "get_self_context": lambda: ctx})

    def test_known_true_with_a_value_shows_and_reflects_it(self):
        row = board_visibility_row(self._shell(
            _caps("known", True), {"visible_on_board": True}))
        self.assertEqual(row, {"show": True, "checked": True})
        row = board_visibility_row(self._shell(
            _caps("known", True), {"visible_on_board": False}))
        self.assertEqual(row, {"show": True, "checked": False})

    def test_known_false_hides(self):
        row = board_visibility_row(self._shell(
            _caps("known", False), {"visible_on_board": True}))
        self.assertEqual(row, {"show": False, "checked": False})

    def test_unknown_hides(self):
        row = board_visibility_row(self._shell(
            _caps("unknown"), {"visible_on_board": True}))
        self.assertEqual(row, {"show": False, "checked": False})

    def test_absent_capability_key_hides(self):
        row = board_visibility_row(self._shell({}, {"visible_on_board": True}))
        self.assertEqual(row, {"show": False, "checked": False})

    def test_missing_or_non_boolean_value_hides_and_never_guesses(self):
        for ctx in ({}, {"visible_on_board": None},
                    {"visible_on_board": "true"}, {"visible_on_board": 1}):
            row = board_visibility_row(self._shell(_caps("known", True), ctx))
            self.assertEqual(row, {"show": False, "checked": False},
                             msg=f"ctx={ctx!r}")

    def test_hidden_reads_nothing_from_self_context(self):
        shell = self._shell(_caps("unknown"), {"visible_on_board": True})
        row = board_visibility_row(shell)
        self.assertFalse(row["show"])
        self.assertNotIn("get_self_context",
                         [name for name, _args in shell.calls])

    def test_toggle_routes_through_the_named_handler(self):
        shell = _Shell({"on_board_visibility": lambda visible: {"ok": True}})
        self.assertTrue(board_visibility_toggled(shell, True))
        self.assertIn(("on_board_visibility", (True,)), shell.calls)
        self.assertTrue(board_visibility_toggled(shell, False))
        self.assertIn(("on_board_visibility", (False,)), shell.calls)

    def test_set_board_visibility_posts_the_rpc(self):
        with _Fixture() as fx:
            fx.route("/rest/v1/rpc/set_board_visibility",
                     (200, {"visible_on_board": False}))
            self.assertFalse(set_board_visibility(post_json, fx.endpoint,
                                                  _session(), False))
            self.assertEqual(fx.seen[0]["path"],
                             "/rest/v1/rpc/set_board_visibility")
            self.assertEqual(fx.seen[0]["body"], {"p_visible": False})


if __name__ == "__main__":
    unittest.main()
