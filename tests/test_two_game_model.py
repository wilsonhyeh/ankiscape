# tests/test_two_game_model.py - D1-D4 two-game model, S2/S7/S12/S13.
"""The profile keeps one active-game pointer with two game slots: the offline
(local-only) game and the account game. Login adopts the account game; the
local game never uploads and nothing is imported or deleted. This suite pins
the pure pieces the coordinator and the register notice share:

  - S13: the operational "an active local game exists" predicate that chooses
    the register notice variant (pointer + binding + journal existence only;
    no network, no mint, no engine build).
  - S2: the register-notice copy for both variants, with the board-visibility
    disclosure and the two-separate-stat-sets sentence.
  - S7/S11/S12: the two-game slots, the adoptable-reply rule (created KEY
    present, never created is True), and the binding that lands in the account
    game's journal carrying the account game's uuid.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

from evolved.journal import Journal
from evolved.link import (LinkResult, link_reply_adoptable, persist_binding,
                          read_binding)
from evolved.onboarding import (active_local_game, coordinator_offer_uuid,
                                link_slots, register_notice_variant)
from evolved.ui.account import (REGISTER_NOTICE_FIRST_RUN,
                                REGISTER_NOTICE_UPGRADE, register_notice_text)

LOCAL = "11111111-1111-1111-1111-111111111111"
ACCOUNT = "22222222-2222-2222-2222-222222222222"


class TestNoticeVariantPredicate(unittest.TestCase):
    """S13: the operational variant chooser against pointer/binding/slots."""

    def test_no_pointer_is_first_run(self):
        self.assertEqual(
            register_notice_variant(pointer=None, journal_exists=False,
                                    journal_has_binding=False), "first_run")

    def test_pointer_without_uuid_is_first_run(self):
        self.assertEqual(
            register_notice_variant(pointer={"version": 1, "game_uuid": ""},
                                    journal_exists=False,
                                    journal_has_binding=False), "first_run")

    def test_account_game_active_is_first_run(self):
        # The active game IS the account game: nothing offline to protect.
        self.assertEqual(
            register_notice_variant(
                pointer={"game_uuid": ACCOUNT, "account_game_uuid": ACCOUNT},
                journal_exists=True, journal_has_binding=True,
                account_game_uuid=ACCOUNT), "first_run")

    def test_active_game_with_a_binding_is_the_account_game(self):
        self.assertFalse(active_local_game(
            pointer={"game_uuid": LOCAL}, journal_exists=True,
            journal_has_binding=True, account_game_uuid=ACCOUNT))

    def test_missing_journal_is_first_run(self):
        self.assertFalse(active_local_game(
            pointer={"game_uuid": LOCAL}, journal_exists=False,
            journal_has_binding=False, account_game_uuid=ACCOUNT))

    def test_local_game_with_a_journal_is_upgrade(self):
        self.assertEqual(
            register_notice_variant(
                pointer={"game_uuid": LOCAL, "local_game_uuid": LOCAL,
                         "account_game_uuid": ""},
                journal_exists=True, journal_has_binding=False,
                account_game_uuid=""), "upgrade")

    def test_free_function_and_predicate_agree(self):
        self.assertTrue(active_local_game(
            pointer={"game_uuid": LOCAL}, journal_exists=True,
            journal_has_binding=False, account_game_uuid=ACCOUNT))


class TestRegisterNoticeCopy(unittest.TestCase):
    """S2/D4/D7: both variants, level 1 / 0 XP, the disclosure, two stats."""

    def test_first_run_variant(self):
        text = register_notice_text("first_run")
        self.assertEqual(text, REGISTER_NOTICE_FIRST_RUN)
        self.assertIn("level 1 with 0 XP", text)
        self.assertIn("public hiscores board", text)
        self.assertNotIn("NOT transferred", text)

    def test_upgrade_variant(self):
        text = register_notice_text("upgrade")
        self.assertEqual(text, REGISTER_NOTICE_UPGRADE)
        self.assertIn("level 1 with 0 XP", text)
        self.assertIn("NOT transferred", text)
        self.assertIn("never uploaded", text)
        self.assertIn("The two games keep separate stats.", text)
        self.assertIn("public hiscores board", text)

    def test_disclosure_sentence_identical_in_both_variants(self):
        sentence = ("You can choose whether your progress is shown on the "
                    "public hiscores board.")
        self.assertTrue(REGISTER_NOTICE_FIRST_RUN.endswith(sentence))
        self.assertTrue(REGISTER_NOTICE_UPGRADE.endswith(sentence))

    def test_unknown_variant_falls_back_to_first_run(self):
        self.assertEqual(register_notice_text("nonsense"),
                         REGISTER_NOTICE_FIRST_RUN)


class TestAdoptableReply(unittest.TestCase):
    """S11: `created` is a key-presence rule, never `created is True`."""

    def test_created_false_with_the_key_present_is_adoptable(self):
        result = LinkResult(True, "already_linked", created=False,
                            created_present=True, game_uuid=ACCOUNT)
        self.assertTrue(link_reply_adoptable(result))

    def test_old_server_reply_without_the_key_is_not_adoptable(self):
        result = LinkResult(True, "linked", created=False,
                            created_present=False, game_uuid=ACCOUNT)
        self.assertFalse(link_reply_adoptable(result))

    def test_uuid_shape_is_required(self):
        result = LinkResult(True, "linked", created=True,
                            created_present=True, game_uuid="game-fixture-1")
        self.assertFalse(link_reply_adoptable(result))
        result = LinkResult(True, "linked", created=True,
                            created_present=True, game_uuid="")
        self.assertFalse(link_reply_adoptable(result))

    def test_failed_link_is_never_adoptable(self):
        result = LinkResult(False, "offline", created_present=True,
                            game_uuid=ACCOUNT)
        self.assertFalse(link_reply_adoptable(result))


class TestTwoGameSlots(unittest.TestCase):
    """S7/R14 step 5: the slots the pointer records after a login."""

    def test_login_from_a_local_game_keeps_both_slots(self):
        self.assertEqual(
            link_slots(prior_active=LOCAL, account_game_uuid=ACCOUNT),
            {"local_game_uuid": LOCAL, "account_game_uuid": ACCOUNT})

    def test_account_first_login_has_no_local_slot(self):
        self.assertEqual(
            link_slots(prior_active="", account_game_uuid=ACCOUNT),
            {"account_game_uuid": ACCOUNT})

    def test_relogin_does_not_move_the_account_game_into_the_local_slot(self):
        self.assertEqual(
            link_slots(prior_active=ACCOUNT, account_game_uuid=ACCOUNT),
            {"account_game_uuid": ACCOUNT})


class TestCoordinatorOfferUuid(unittest.TestCase):
    """S12: a null pointer offers any uuid; a real engine keeps its uuid."""

    def test_engine_uuid_is_offered_when_a_local_game_exists(self):
        self.assertEqual(
            coordinator_offer_uuid(engine_uuid=LOCAL, pointer_uuid=LOCAL,
                                   engine_available=True), LOCAL)

    def test_no_local_game_offers_a_fresh_uuid(self):
        offered = coordinator_offer_uuid(engine_uuid="", pointer_uuid="",
                                         engine_available=False)
        self.assertTrue(offered)
        self.assertNotEqual(offered, "")
        self.assertEqual(len(offered), 36)  # uuid4 text shape

    def test_a_game_that_failed_to_load_keeps_the_early_return(self):
        self.assertEqual(
            coordinator_offer_uuid(engine_uuid="", pointer_uuid=LOCAL,
                                   engine_available=False), "")


class TestBindingLandsInTheAccountJournal(unittest.TestCase):
    """S7: the binding records the account game's uuid in its own journal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.account_journal = Journal(os.path.join(self.tmp.name, "account",
                                                    "game.sqlite3"))
        self.addCleanup(self.account_journal.close)
        self.local_journal = Journal(os.path.join(self.tmp.name, "local",
                                                  "game.sqlite3"))
        self.addCleanup(self.local_journal.close)

    def test_binding_records_the_account_uuid_and_journal(self):
        result = LinkResult(True, "linked", created=True, created_present=True,
                            game_uuid=ACCOUNT)
        self.assertTrue(persist_binding(self.account_journal, result,
                                        game_uuid=result.game_uuid,
                                        user_id="u-1",
                                        endpoint_project="proj"))
        binding = read_binding(self.account_journal)
        self.assertEqual(binding["game_uuid"], ACCOUNT)
        self.assertIsNone(read_binding(self.local_journal))

    def test_account_journal_outbox_survives_a_relogin_drain(self):
        # S4 + S7: the drain guard must never touch a bound journal, so a
        # re-login that retires the demoted game cannot delete the account
        # game's pending operations.
        self.account_journal.set_metadata(
            "account_binding",
            '{"game_uuid": "%s", "user_id": "u-1"}' % ACCOUNT)
        self.account_journal.append_operation({
            "op_id": "acct-op-1", "game_uuid": ACCOUNT,
            "device_id": "dev-a", "device_seq": 1, "lamport": 1,
            "kind": "review_award", "payload": {"review_key": "rk-1"}})
        self.assertEqual(self.account_journal.retire_outbox(), 0)
        self.assertEqual(self.account_journal.count_pending_operations(), 1)


class TestAccountFirstLogin(unittest.TestCase):
    """S12: a profile with no game at all adopts the account game.

    Drives the real addon coordinator (headless, aqt stubs) against the
    loopback fixture server: the offered uuid is any fresh uuid, the account
    game is materialized and activated, no game is minted for the profile
    before the choice, and no local slot appears.
    """

    def setUp(self):
        from tests.test_bricked_recovery import (
            ACCOUNT_UUID, _DummyCol, _DummyMW, _DummyPM, _FixtureServer,
            _Session, _install_runtime_fakes, _load_addon_as_package)

        self.account_uuid = ACCOUNT_UUID
        self._orig_modules = dict(sys.modules)
        _install_runtime_fakes()
        self.addCleanup(self._restore_modules)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = _FixtureServer()
        self.addCleanup(self.fixture.close)
        self.col = _DummyCol({"ankiscape_evolved_device_id": "dev-test"})
        self.addon = _load_addon_as_package("ankiscape_two_game_model")
        self.addon.mw = _DummyMW(self.col, _DummyPM(self.tmp.name))
        self.addon._evolved_endpoint = lambda: self.fixture.endpoint
        self.session = _Session()
        self.addon._evolved_profile_session = lambda: self.session
        self.addon._EVOLVED_CTX["profile_session"] = self.session
        self.addon._EVOLVED_CTX["user_id"] = self.session.user_id

    def _restore_modules(self):
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def test_account_first_login_materializes_and_activates(self):
        from evolved.journal import journal_path_for_profile
        from evolved.link import read_binding

        # R13: a null pointer never mints an engine.
        self.assertIsNone(self.addon._ensure_evolved_engine())
        self.assertTrue(self.addon._evolved_post_auth_coordinator("login"))

        pointer = self.col.get_config("ankiscape_evolved_player_data")
        self.assertEqual(pointer["game_uuid"], self.account_uuid)
        self.assertEqual(pointer["account_game_uuid"], self.account_uuid)
        self.assertNotIn("local_game_uuid", pointer)

        offered = self.fixture.links[0]["p_game_uuid"]
        self.assertTrue(offered)
        self.assertNotEqual(offered, self.account_uuid)

        # No game was minted for the profile or for the offered uuid; the
        # account game's journal was created by materialization.
        self.assertFalse(os.path.exists(
            journal_path_for_profile(self.tmp.name, offered)))
        self.assertTrue(os.path.exists(
            journal_path_for_profile(self.tmp.name, self.account_uuid)))
        self.assertEqual(self.addon._EVOLVED_CTX["engine"].cfg.game_uuid,
                         self.account_uuid)
        self.assertEqual(read_binding(self.addon._EVOLVED_CTX["journal"]
                                      )["game_uuid"], self.account_uuid)


if __name__ == "__main__":
    unittest.main()
