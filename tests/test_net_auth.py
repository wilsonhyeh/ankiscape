import unittest

from evolved.auth import MemorySession, normalize_username
from evolved.net import Endpoint, NetError, _check_url, _map_http_error
import urllib.error


class TestAuthNet(unittest.TestCase):
    def test_username_normalization(self):
        self.assertEqual(normalize_username("  Wilson_HYEH "), "wilson_hyeh")
        with self.assertRaises(ValueError):
            normalize_username("ab")
        with self.assertRaises(ValueError):
            normalize_username("bad name!")

    def test_memory_session_cleared(self):
        session = MemorySession()
        session.set(access_token="a", refresh_token="r", user_id="u", username="w")
        self.assertTrue(session.logged_in)
        session.clear()
        self.assertFalse(session.logged_in)

    def test_https_required_loopback_exception(self):
        prod = Endpoint(base_url="https://api.example.com", project_key="pub")
        self.assertTrue(_check_url(prod, "/rest/v1").startswith("https://"))
        with self.assertRaises(NetError):
            _check_url(Endpoint(base_url="http://api.example.com", project_key="k"), "/x")
        dev = Endpoint(base_url="http://127.0.0.1:54321", project_key="k",
                       allow_http_loopback=True)
        self.assertTrue(_check_url(dev, "/auth/v1/token").startswith("http://"))

    def test_http_error_mapping(self):
        err = urllib.error.HTTPError("http://x", 429, "Too Many", {}, None)
        mapped = _map_http_error(err)
        self.assertEqual(mapped.kind, "rate_limited")
        err422 = urllib.error.HTTPError("http://x", 422, "Unprocessable", {}, None)
        self.assertEqual(_map_http_error(err422).kind, "invalid")


if __name__ == "__main__":
    unittest.main()
