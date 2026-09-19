# tests/test_bricked_recovery.py - S4/H4: the un-brick path, synthetic profile.
"""A "bricked" install is a local game whose journal has stray pending
operations and no account_binding. Logging in must demote that game to the
offline slot, drain its outbox (rows deleted, operations retained, acked never
set), and adopt the account game — all without uploading one byte of the old
local history.

The profile is built from scratch through the Journal API (no artifact under
artifacts/account-repair/private/** is ever read) and the real post-auth
coordinator is replayed against a loopback fixture server carrying the 0011
link contract.
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

LOCAL_UUID = "33333333-3333-3333-3333-333333333333"
ACCOUNT_UUID = "44444444-4444-4444-4444-444444444444"


# --------------------------------------------------------------------- fakes
class _FakeHooks:
    def __init__(self):
        self.overview_did_refresh = []
        self.overview_will_refresh = []
        self.reviewer_did_show_question = []
        self.reviewer_did_show_answer = []
        self.webview_did_receive_js_message = []


class _FakeReviewer:
    def _answerCard(self, ease):
        return None


def _install_runtime_fakes():
    aqt = types.ModuleType("aqt")
    aqt.mw = None
    aqt.gui_hooks = _FakeHooks()
    aqt_reviewer = types.ModuleType("aqt.reviewer")
    aqt_reviewer.Reviewer = _FakeReviewer
    sys.modules["aqt"] = aqt
    sys.modules["aqt.reviewer"] = aqt_reviewer
    anki_hooks = types.ModuleType("anki.hooks")

    def addHook(_name, _fn):
        return None

    def wrap(old, new, _mode):
        def _wrapped(self, ease):
            return new(self, ease, old)
        return _wrapped

    anki_hooks.addHook = addHook
    anki_hooks.wrap = wrap
    sys.modules["anki.hooks"] = anki_hooks


def _load_addon_as_package(mod_name="ankiscape_bricked_recovery"):
    root = Path(ROOT)
    init_py = root / "__init__.py"
    loader = SourceFileLoader(mod_name, str(init_py))
    spec = spec_from_loader(mod_name, loader, is_package=True)
    mod = module_from_spec(spec)
    mod.__path__ = [str(root)]  # type: ignore[attr-defined]
    sys.modules[mod_name] = mod
    loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------- fixture
class _FixtureServer:
    """Loopback server carrying the post-0011 link/context contract."""

    def __init__(self):
        self.submitted = []
        self.links = []
        self.seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(_self, *args):
                pass

            def _send(_self, status, payload):
                raw = json.dumps(payload).encode("utf-8")
                _self.send_response(status)
                _self.send_header("Content-Type", "application/json")
                _self.send_header("Content-Length", str(len(raw)))
                _self.end_headers()
                _self.wfile.write(raw)

            def do_POST(_self):  # noqa: N802 (http.server API)
                length = int(_self.headers.get("Content-Length") or 0)
                raw = _self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    body = {}
                path = _self.path.split("?")[0]
                self.seen.append(path)
                if path == "/rest/v1/rpc/link_game":
                    self.links.append(body)
                    return _self._send(200, {"game_uuid": ACCOUNT_UUID,
                                             "created": True, "resumed": False})
                if path == "/rest/v1/rpc/evolved_capabilities":
                    return _self._send(200, {"protocol_version": 2,
                                             "authoritative_scoring": True,
                                             "board_visibility": True})
                if path == "/rest/v1/rpc/self_context":
                    return _self._send(200, {"username": "accttest01",
                                             "is_test": False,
                                             "visible_on_board": True})
                if path == "/rest/v1/rpc/fetch_operations":
                    return _self._send(200, {"operations": [],
                                             "next_cursor": 0})
                if path == "/rest/v1/rpc/submit_operations":
                    ops = body.get("p_ops") or []
                    self.submitted.extend(str(op.get("op_id")) for op in ops)
                    return _self._send(200, {"accepted": [],
                                             "conflicts": [], "applied": 0})
                return _self._send(404, {"error": "not_found"})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def endpoint(self):
        from evolved.net import Endpoint
        return Endpoint(
            base_url=f"http://127.0.0.1:{self.server.server_port}",
            project_key="fixture-anon", allow_http_loopback=True)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class _DummyCol:
    def __init__(self, store=None):
        self._store = dict(store or {})

    def get_config(self, key, default=None):
        return self._store.get(key, default)

    def set_config(self, key, value, **kwargs):
        self._store[key] = value


class _DummyPM:
    def __init__(self, folder):
        self._folder = folder

    def profileFolder(self):
        return self._folder


class _DummyMW:
    def __init__(self, col, pm):
        self.col = col
        self.pm = pm


class _Session:
    logged_in = True
    access_token = "fixture-access-1"

    def __init__(self, user_id="user-1"):
        self.user_id = user_id

    def refresh_once(self):
        return False


class TestBrickedRecovery(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)
        _install_runtime_fakes()
        self.addCleanup(self._restore_modules)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile_dir = self.tmp.name
        self.fixture = _FixtureServer()
        self.addCleanup(self.fixture.close)

        import time
        self.col = _DummyCol({
            "ankiscape_evolved_device_id": "dev-test",
            "ankiscape_evolved_player_data": {
                "version": 1, "game_uuid": LOCAL_UUID,
                "activated_at": int(time.time()), "snapshot_revision": 0,
                "preset": {"skill": "mining",
                           "effective_ts": int(time.time())}},
        })
        self.addon = _load_addon_as_package()
        self.addon.mw = _DummyMW(self.col, _DummyPM(self.profile_dir))
        self.addon._evolved_endpoint = lambda: self.fixture.endpoint
        self.session = _Session()
        self.addon._evolved_profile_session = lambda: self.session
        # The coordinator reads the live session slot directly.
        self.addon._EVOLVED_CTX["profile_session"] = self.session
        self.addon._EVOLVED_CTX["user_id"] = self.session.user_id

    def _restore_modules(self):
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def _build_bricked_local_journal(self, count: int = 3):
        from evolved.journal import Journal, journal_path_for_profile

        path = journal_path_for_profile(self.profile_dir, LOCAL_UUID)
        journal = Journal(path)
        op_ids = []
        for index in range(count):
            op_id = f"local-op-{index + 1}"
            op_ids.append(op_id)
            journal.append_operation({
                "op_id": op_id, "game_uuid": LOCAL_UUID,
                "device_id": "dev-local", "device_seq": index + 1,
                "lamport": index + 1, "kind": "review_award",
                "payload": {"review_key": f"rk-{index + 1}"}})
        # No account_binding, no checkpoint: the bricked shape.
        self.assertIsNone(journal.get_metadata("account_binding"))
        self.assertEqual(journal.count_pending_operations(), count)
        journal.close()
        return path, op_ids

    def test_unbound_local_game_drains_without_uploading(self):
        from evolved.data import load_rules
        from evolved.journal import Journal, journal_path_for_profile
        from evolved.link import read_binding
        from evolved.reducer import replay

        path, op_ids = self._build_bricked_local_journal()
        rules = load_rules()
        journal = Journal(path)
        before = journal.all_operations()
        before_xp = replay(before, rules, LOCAL_UUID)["xp_micro"]
        journal.close()

        engine = self.addon._ensure_evolved_engine()
        self.assertIsNotNone(engine)
        self.assertEqual(engine.cfg.game_uuid, LOCAL_UUID)
        # D2: the local game never builds a sync service, even signed in.
        self.assertIsNone(self.addon._evolved_sync_service())

        self.assertTrue(self.addon._evolved_post_auth_coordinator("login"))

        # Zero uploads: no submit ever carried a local op id.
        self.assertEqual(self.fixture.submitted, [])
        self.assertEqual(self.fixture.links[0]["p_game_uuid"], LOCAL_UUID,
                         "the local uuid is only the offered argument")

        # The account game is active with both slots recorded.
        pointer = self.col.get_config("ankiscape_evolved_player_data")
        self.assertEqual(pointer["game_uuid"], ACCOUNT_UUID)
        self.assertEqual(pointer["account_game_uuid"], ACCOUNT_UUID)
        self.assertEqual(pointer["local_game_uuid"], LOCAL_UUID)

        # The demoted local journal drained: outbox emptied, operations
        # retained unchanged, acked never set, retirement recorded.
        local = Journal(path)
        try:
            self.assertEqual(local.count_pending_operations(), 0)
            after = local.all_operations()
            self.assertEqual([op["op_id"] for op in after], op_ids)
            self.assertEqual(replay(after, rules, LOCAL_UUID)["xp_micro"],
                             before_xp)
            self.assertEqual(local.get_metadata("outbox_retired"), "1")
            acked = [int(row["acked"]) for row in local._conn.execute(
                "SELECT acked FROM operations").fetchall()]
            self.assertEqual(acked, [0] * len(op_ids))
        finally:
            local.close()

        # The binding lives in the account game's journal, carrying B.
        account_journal = self.addon._EVOLVED_CTX["journal"]
        self.assertEqual(read_binding(account_journal)["game_uuid"],
                         ACCOUNT_UUID)

        # S1: both payloads were fetched eagerly (construction + login) and
        # cached as raw/tri-state for the shell surface.
        self.assertIn("/rest/v1/rpc/evolved_capabilities", self.fixture.seen)
        self.assertIn("/rest/v1/rpc/self_context", self.fixture.seen)
        self.assertEqual(
            self.addon._EVOLVED_CTX["capabilities"]["board_visibility"],
            {"state": "known", "value": True})
        self.assertTrue(
            self.addon._EVOLVED_CTX["self_context"]["visible_on_board"])

        # The account game is the one draining: its sync service is B's.
        svc = self.addon._evolved_sync_service()
        self.assertIsNotNone(svc)
        self.assertEqual(svc.game_uuid, ACCOUNT_UUID)


if __name__ == "__main__":
    unittest.main()
