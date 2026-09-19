import json
import os
import tempfile
import unittest

from evolved.journal import Journal
from evolved.link import (binding_metadata, ensure_link, persist_binding,
                          read_binding)
from evolved.net import Endpoint, NetError


class _Session:
    def __init__(self, token="tok", user_id="u1"):
        self.access_token = token
        self.user_id = user_id
        self.refreshes = 0

    def refresh(self):
        self.refreshes += 1
        self.access_token = "tok-new"
        return True


def _endpoint():
    return Endpoint(base_url="https://api.example", project_key="pub")


def _post_result(payload):
    def _post(endpoint, path, body, access_token=None):
        assert path == "/rest/v1/rpc/link_game"
        return payload
    return _post


def _post_error(kind, detail="", code=""):
    def _post(endpoint, path, body, access_token=None):
        raise NetError(kind, detail, code=code)
    return _post


class TestEnsureLink(unittest.TestCase):
    def test_new_link(self):
        result = ensure_link(_post_result({"game_uuid": "g", "resumed": False}),
                             _endpoint(), _Session(), game_uuid="g")
        self.assertTrue(result.linked)
        self.assertEqual(result.state, "linked")
        self.assertFalse(result.resumed)

    def test_same_game_resumes(self):
        result = ensure_link(_post_result({"resumed": True}), _endpoint(),
                             _Session(), game_uuid="g")
        self.assertTrue(result.linked)
        self.assertEqual(result.state, "already_linked")

    def test_game_claimed_points_at_updating(self):
        result = ensure_link(_post_error("conflict",
                                         json.dumps({"code": "game_claimed"})),
                             _endpoint(), _Session(), game_uuid="g")
        self.assertFalse(result.ok)
        self.assertEqual(result.state, "game_claimed")
        self.assertEqual(result.message,
                         "Update AnkiScape to set up sync for this account.")

    def test_game_mismatch_and_no_profile(self):
        mismatch = ensure_link(_post_error("forbidden", '{"code":"game_mismatch"}'),
                               _endpoint(), _Session(), game_uuid="g")
        self.assertEqual(mismatch.state, "game_mismatch")
        self.assertEqual(mismatch.message,
                         "Update AnkiScape to set up sync for this account.")
        missing = ensure_link(_post_error("conflict", '{"code":"no_profile"}'),
                              _endpoint(), _Session(), game_uuid="g")
        self.assertEqual(missing.state, "no_profile")

    def test_unverified_is_explicit(self):
        result = ensure_link(_post_error("forbidden", '{"code":"unverified"}'),
                             _endpoint(), _Session(), game_uuid="g")
        self.assertEqual(result.state, "unverified")

    def test_offline_is_retryable_not_ownership(self):
        result = ensure_link(_post_error("connection", "reset"), _endpoint(),
                             _Session(), game_uuid="g")
        self.assertEqual(result.state, "offline")
        self.assertFalse(result.linked)

    def test_unauthorized_refreshes_once(self):
        session = _Session()
        attempts = {"n": 0}

        def _post(endpoint, path, body, access_token=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise NetError("unauthorized", "expired")
            return {"resumed": True}

        result = ensure_link(_post, _endpoint(), session, game_uuid="g",
                             refresh=session.refresh)
        self.assertTrue(result.linked)
        self.assertEqual(session.refreshes, 1)
        self.assertEqual(attempts["n"], 2)

    def test_logged_out_when_no_token(self):
        result = ensure_link(_post_error("unauthorized", ""), _endpoint(),
                             _Session(token=None), game_uuid="g")
        self.assertEqual(result.state, "logged_out")

    def test_unconfigured(self):
        result = ensure_link(_post_result({}), None, _Session(), game_uuid="g")
        self.assertEqual(result.state, "unconfigured")


class TestServerOwnedUuidContract(unittest.TestCase):
    """S11: the server owns the game uuid; `created` is read by key presence.

    `created == True` is never required: a repeat call legitimately returns
    `created: false, resumed: true` and must still be adoptable.
    """

    B = "22222222-2222-2222-2222-222222222222"

    def test_first_link_adopts_the_returned_uuid(self):
        result = ensure_link(
            _post_result({"game_uuid": self.B, "created": True,
                          "resumed": False}),
            _endpoint(), _Session(), game_uuid="offered-local")
        self.assertTrue(result.linked)
        self.assertEqual(result.state, "linked")
        self.assertTrue(result.created_present)
        self.assertTrue(result.created)
        self.assertFalse(result.resumed)
        self.assertEqual(result.game_uuid, self.B)

    def test_repeat_link_is_created_false_and_still_adoptable(self):
        result = ensure_link(
            _post_result({"game_uuid": self.B, "created": False,
                          "resumed": True}),
            _endpoint(), _Session(), game_uuid="offered-local")
        self.assertTrue(result.linked)
        self.assertEqual(result.state, "already_linked")
        self.assertTrue(result.created_present)
        self.assertFalse(result.created)
        self.assertTrue(result.resumed)
        self.assertEqual(result.game_uuid, self.B)

    def test_old_server_reply_without_the_created_key_is_not_adoptable(self):
        result = ensure_link(_post_result({"game_uuid": "g", "resumed": True}),
                             _endpoint(), _Session(), game_uuid="g")
        # Login never fails on this: the reply simply cannot be adopted.
        self.assertTrue(result.linked)
        self.assertFalse(result.created_present)

    def test_reply_without_a_game_uuid_keeps_login_alive(self):
        result = ensure_link(_post_result({"created": True}), _endpoint(),
                             _Session(), game_uuid="g")
        self.assertTrue(result.linked)
        self.assertEqual(result.game_uuid, "")


class TestBindingPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(os.path.join(self.tmp.name, "game.sqlite3"))
        self.addCleanup(self.journal.close)

    def test_binding_roundtrip_and_shape(self):
        from evolved.link import LinkResult
        result = LinkResult(True, "linked")
        self.assertTrue(persist_binding(self.journal, result, game_uuid="g",
                                        user_id="u1",
                                        endpoint_project="https://api"))
        binding = read_binding(self.journal)
        self.assertEqual(binding, binding_metadata(game_uuid="g", user_id="u1",
                                                   endpoint_project="https://api"))
        self.assertEqual(set(binding), {"game_uuid", "user_id",
                                        "endpoint_project"})
        # No credentials are ever stored.
        raw = self.journal.get_metadata("account_binding")
        self.assertNotIn("token", raw.lower())
        self.assertNotIn("password", raw.lower())

    def test_failed_link_persists_nothing(self):
        from evolved.link import LinkResult
        self.assertFalse(persist_binding(self.journal,
                                         LinkResult(False, "game_mismatch"),
                                         game_uuid="g", user_id="u1",
                                         endpoint_project="https://api"))
        self.assertIsNone(read_binding(self.journal))

    def test_malformed_binding_reads_none(self):
        self.journal.set_metadata("account_binding", "not json")
        self.assertIsNone(read_binding(self.journal))
        self.journal.set_metadata("account_binding", '{"game_uuid": "g"}')
        self.assertIsNone(read_binding(self.journal))


if __name__ == "__main__":
    unittest.main()
