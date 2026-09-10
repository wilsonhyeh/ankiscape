import os
import tempfile
import unittest

from evolved.service import query_hiscores
from evolved.session_store import (ProfileSession, ProdConfig,
                                   resolve_endpoint)


class _Session:
    def __init__(self, token="tok"):
        self.access_token = token
        self.refresh_calls = 0

    def refresh(self):
        self.refresh_calls += 1
        self.access_token = "tok-new"
        return True


class TestResolveEndpoint(unittest.TestCase):
    def test_dev_needs_anon_key(self):
        self.assertIsNone(resolve_endpoint(dev=True, anon_key=""))
        ep = resolve_endpoint(dev=True, anon_key="pub")
        self.assertIsNotNone(ep)
        self.assertTrue(ep.base_url.startswith("http://127.0.0.1"))

    def test_prod_unconfigured_fails_closed(self):
        self.assertIsNone(resolve_endpoint(
            dev=False, prod=ProdConfig(base_url="", anon_key="")))
        self.assertIsNone(resolve_endpoint(
            dev=False, prod=ProdConfig(base_url="https://x.supabase.co",
                                       anon_key="")))
        ep = resolve_endpoint(dev=False, prod=ProdConfig(
            base_url="https://x.supabase.co", anon_key="pub"))
        self.assertIsNotNone(ep)
        assert ep is not None
        self.assertTrue(ep.base_url.startswith("https://"))


class TestProfileSession(unittest.TestCase):
    def test_clear_and_login_state(self):
        sess = ProfileSession(generation=5)
        self.assertFalse(sess.logged_in)
        sess.session.set(access_token="a", refresh_token="r", user_id="u")
        self.assertTrue(sess.logged_in)
        self.assertEqual(sess.user_id, "u")
        sess.clear()
        self.assertFalse(sess.logged_in)

    def test_refresh_serialized_single_flight(self):
        calls = {"n": 0}

        def _slow(session):
            calls["n"] += 1
            import time as _t
            _t.sleep(0.2)
            return True

        sess = ProfileSession(generation=5)
        sess.bind_refresh(_slow)
        import threading as _th
        results = []
        threads = [_th.Thread(target=lambda: results.append(sess.refresh_once()))
                   for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # At most 2 ran (one holder + waiters reading the result); the
        # underlying callable ran exactly once per holder acquisition.
        self.assertLessEqual(calls["n"], 2)
        self.assertTrue(all(results))

    def test_no_refresh_fn_fails_closed(self):
        sess = ProfileSession(generation=5)
        self.assertFalse(sess.refresh_once())


class TestQueryHiscores(unittest.TestCase):
    def test_rows_passthrough(self):
        rows = [{"rank": 1, "username": "Amy", "xp": "5000"}]

        def _post(endpoint, path, payload, access_token=None,
                  response_shape="object"):
            self.assertTrue(path.endswith("hiscores"))
            self.assertEqual(payload["p_skill"], "mining")
            self.assertEqual(response_shape, "array")
            return list(rows)

        from evolved.net import Endpoint
        out = query_hiscores(_post, Endpoint(base_url="https://x",
                                             project_key="k"),
                             _Session(), skill="mining", limit=10)
        self.assertEqual(out[0]["username"], "Amy")

    def test_malformed_raises(self):
        from evolved.net import Endpoint, NetError
        with self.assertRaises(NetError):
            query_hiscores(lambda *a, **k: {"not": "a list"},
                           Endpoint(base_url="https://x", project_key="k"),
                           _Session(), skill="mining")


if __name__ == "__main__":
    unittest.main()
