import unittest

from evolved.data import load_rules
from evolved.ui import hiscores_model as hm
from evolved.ui.menu_model import format_hiscores_rows

MICRO = 1_000_000


def board(pairs):
    """[(name, whole_xp)] -> competition-ranked formatted rows (xp desc, then
    name), the same shape the server + format_hiscores_rows produce."""
    ordered = sorted(pairs, key=lambda p: (-p[1], p[0].lower()))
    rows, last, rank = [], None, 0
    for i, (name, xp) in enumerate(ordered, start=1):
        if xp != last:
            rank, last = i, xp
        rows.append({"rank": rank, "username": name, "xp": xp * MICRO,
                     "is_demo": False})
    return format_hiscores_rows(rows)


class TestLevelsAndFormatting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.th = load_rules()["thresholds"]

    def test_level_boundaries(self):
        self.assertEqual(hm.level_of(0, self.th), 1)
        self.assertEqual(hm.level_of(82 * MICRO, self.th), 1)
        self.assertEqual(hm.level_of(83 * MICRO, self.th), 2)
        self.assertEqual(hm.level_of(13_034_431 * MICRO, self.th), 99)
        self.assertEqual(hm.level_of(10 ** 15, self.th), 99)

    def test_level_without_table_is_one(self):
        self.assertEqual(hm.level_of(10 ** 12, []), 1)

    def test_whole_xp_truncates_never_rounds_up(self):
        self.assertEqual(hm.format_whole_xp(1_180_187_500), "1,180")
        self.assertEqual(hm.format_whole_xp(999_999), "0")
        self.assertEqual(hm.format_whole_xp("10300500000"), "10,300")
        self.assertEqual(hm.format_whole_xp(None), "0")
        self.assertEqual(hm.format_whole_xp("junk"), "0")

    def test_ordinal(self):
        self.assertEqual([hm.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 101, 111)],
                         ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th",
                          "21st", "22nd", "101st", "111th"])


class TestTrainedSplit(unittest.TestCase):
    def test_zero_xp_rows_are_counted_not_listed(self):
        rows = board([("a", 500), ("b", 0), ("c", 0), ("d", 20)])
        trained, untrained = hm.split_trained(rows)
        self.assertEqual([r["username"] for r in trained], ["a", "d"])
        self.assertEqual(len(untrained), 2)

    def test_untrained_copy(self):
        self.assertEqual(hm.untrained_note("mining", 0), "")
        self.assertEqual(hm.untrained_note("mining", 1),
                         "1 player hasn't trained Mining yet.")
        self.assertEqual(hm.untrained_note("fishing", 5),
                         "5 players haven't trained Fishing yet.")
        self.assertEqual(hm.untrained_note("overall", 2),
                         "2 players haven't started playing yet.")

    def test_empty_board_copy_says_be_first(self):
        self.assertIn("first", hm.empty_board_copy("crafting", 9).lower())
        self.assertIn("Crafting", hm.empty_board_copy("crafting", 9))
        self.assertEqual(hm.empty_board_copy("crafting", 0), "No players yet.")


class TestStanding(unittest.TestCase):
    def setUp(self):
        self.rows = board([("yae", 10300), ("CAgirl", 1180), ("Legacy", 675),
                           ("src", 116), ("zed", 0), ("amy", 0)])

    def test_gap_targets_the_player_you_must_pass(self):
        st = hm.standing(self.rows, "legacy")
        self.assertEqual(st["rank"], 3)
        self.assertEqual(st["target"], "CAgirl")
        self.assertEqual(st["gap"], (1180 - 675) * MICRO)
        self.assertFalse(st["leader"])

    def test_leader_has_no_target(self):
        st = hm.standing(self.rows, "yae")
        self.assertTrue(st["leader"])
        self.assertIsNone(st["target"])
        self.assertEqual(st["gap"], 0)

    def test_ties_are_not_ahead_of_you(self):
        rows = board([("a", 100), ("b", 50), ("c", 50), ("d", 10)])
        st = hm.standing(rows, "c")
        self.assertEqual(st["rank"], 2)
        self.assertEqual(st["tied_with"], 1)
        self.assertEqual(st["target"], "a")

    def test_target_is_closest_ahead_not_the_leader(self):
        st = hm.standing(self.rows, "src")
        self.assertEqual(st["target"], "Legacy")

    def test_untrained_player_has_no_rank_claim(self):
        st = hm.standing(self.rows, "zed")
        self.assertFalse(st["trained"])
        text = hm.standing_text(st, "mining")
        self.assertEqual(text["headline"], "Not ranked yet.")
        self.assertEqual(text["goal"], "Earn any Mining XP to get on the board.")
        overall = hm.standing_text(hm.standing(self.rows, "zed"), "overall")
        self.assertEqual(overall["goal"], "Earn any XP to get on the board.")

    def test_absent_player(self):
        self.assertIsNone(hm.standing(self.rows, "ghost"))
        self.assertEqual(hm.standing_text(None, "mining")["headline"],
                         "Not on this board yet.")
        self.assertIsNone(hm.standing(self.rows, ""))

    def test_case_insensitive_match(self):
        self.assertEqual(hm.standing(self.rows, "CAGIRL")["rank"], 2)

    def test_text_shapes(self):
        text = hm.standing_text(hm.standing(self.rows, "Legacy"), "mining")
        self.assertEqual(text["headline"], "Rank #3 of 6")
        self.assertEqual(text["goal"], "505 XP to pass CAgirl")
        lead = hm.standing_text(hm.standing(self.rows, "yae"), "mining")
        self.assertIn("lead", lead["goal"])

    def test_sub_one_xp_gap_is_not_zero_xp(self):
        rows = format_hiscores_rows([
            {"rank": 1, "username": "a", "xp": 100 * MICRO + 400_000},
            {"rank": 2, "username": "b", "xp": 100 * MICRO}])
        text = hm.standing_text(hm.standing(rows, "b"), "mining")
        self.assertEqual(text["goal"], "Under 1 XP behind a")

    def test_population_exactness(self):
        full = [{"rank": 1, "username": f"p{i}", "xp": str(i)}
                for i in range(hm.BOARD_LIMIT)]
        st = hm.standing(full, "p5")
        self.assertFalse(st["players_exact"])
        self.assertNotIn(" of ", hm.standing_text(st, "mining")["headline"])


class TestMovement(unittest.TestCase):
    def test_no_previous_means_no_arrows(self):
        rows = board([("a", 5), ("b", 3)])
        self.assertEqual(hm.movement(None, rows), {})
        self.assertEqual(hm.movement({"ranks": {}}, rows), {})
        self.assertEqual(hm.movement("junk", rows), {})

    def test_climb_drop_new_and_flat(self):
        before = board([("a", 10), ("b", 8), ("c", 5)])
        prev = {"ts": 1.0, "ranks": hm.snapshot_ranks(before)}
        now = board([("b", 20), ("a", 10), ("c", 5), ("d", 1)])
        mv = hm.movement(prev, now)
        self.assertEqual(mv["b"], 1)     # 2nd -> 1st
        self.assertEqual(mv["a"], -1)    # 1st -> 2nd
        self.assertEqual(mv["c"], 0)
        self.assertIsNone(mv["d"])       # new
        self.assertEqual(hm.movement_glyph(mv["b"]), ("▲1", "up"))
        self.assertEqual(hm.movement_glyph(mv["a"]), ("▼1", "down"))
        self.assertEqual(hm.movement_glyph(0), ("", "flat"))
        self.assertEqual(hm.movement_glyph(None), ("NEW", "new"))

    def test_snapshot_roll_policy(self):
        self.assertTrue(hm.should_roll_snapshot(None, 100.0))
        self.assertFalse(hm.should_roll_snapshot({"ts": 100.0}, 200.0))
        self.assertTrue(hm.should_roll_snapshot({"ts": 100.0}, 100.0 + 3600))
        self.assertTrue(hm.should_roll_snapshot({"ts": "bad"}, 200.0))


class TestPlayerIndexAndCard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.th = load_rules()["thresholds"]

    def _boards(self):
        skill_xp = {
            "mining": [("yae", 10300), ("amy", 0)],
            "woodcutting": [("amy", 500), ("yae", 0)],
            "smithing": [("yae", 0), ("amy", 0)],
            "crafting": [("yae", 0), ("amy", 0)],
            "fishing": [("yae", 0), ("amy", 0)],
            "cooking": [("yae", 0), ("amy", 0)],
        }
        boards = {s: board(p) for s, p in skill_xp.items()}
        boards["overall"] = board([("yae", 10300), ("amy", 500)])
        return boards

    def test_index_collects_xp_and_ranks(self):
        idx = hm.player_index(self._boards())
        yae = idx["yae"]
        self.assertEqual(yae["xp"]["mining"], 10300 * MICRO)
        self.assertEqual(yae["xp"]["woodcutting"], 0)
        self.assertEqual(yae["ranks"]["overall"], 1)
        self.assertEqual(idx["amy"]["ranks"]["woodcutting"], 1)

    def test_player_missing_from_capped_board_is_unknown_not_zero(self):
        boards = self._boards()
        boards["mining"] = [{"rank": i + 1, "username": f"p{i}",
                             "xp": str((500 - i) * MICRO)}
                            for i in range(hm.BOARD_LIMIT)]
        idx = hm.player_index(boards)
        self.assertIsNone(idx["yae"]["xp"]["mining"])
        self.assertIsNone(hm.total_level(idx["yae"]["xp"], self.th))

    def test_missing_from_complete_board_is_zero(self):
        boards = self._boards()
        boards["cooking"] = board([("yae", 0)])  # amy absent, board complete
        idx = hm.player_index(boards)
        self.assertEqual(idx["amy"]["xp"]["cooking"], 0)

    def test_total_level_sums_six_skills(self):
        idx = hm.player_index(self._boards())
        yae_total = hm.total_level(idx["yae"]["xp"], self.th)
        self.assertEqual(yae_total, hm.level_of(10300 * MICRO, self.th) + 5)

    def test_card_rows_and_summary(self):
        idx = hm.player_index(self._boards())
        rows = hm.card_rows(idx["amy"], self.th)
        self.assertEqual([r["skill"] for r in rows], list(hm.SKILLS))
        wc = next(r for r in rows if r["skill"] == "woodcutting")
        self.assertTrue(wc["trained"])
        self.assertEqual(wc["rank"], 1)
        mining = next(r for r in rows if r["skill"] == "mining")
        self.assertFalse(mining["trained"])
        self.assertIsNone(mining["rank"])       # no rank claimed at 0 XP
        summary = hm.card_summary(idx["amy"], self.th)
        self.assertEqual(summary["best"], "woodcutting")
        self.assertEqual(summary["overall_rank"], 2)
        self.assertEqual(summary["total_xp"], 500 * MICRO)

    def test_demo_accounts_are_always_labeled(self):
        rows = format_hiscores_rows([
            {"rank": 1, "username": "DemoWillow", "xp": 5 * MICRO,
             "is_demo": True},
            {"rank": 2, "username": "Real", "xp": 1 * MICRO,
             "is_demo": False}])
        self.assertEqual(hm.display_name(rows[0]), "DemoWillow  [Demo]")
        self.assertEqual(hm.display_name(rows[1]), "Real")
        idx = hm.player_index({"mining": rows})
        self.assertTrue(idx["demowillow"]["is_demo"])
        self.assertFalse(idx["real"]["is_demo"])
        summary = hm.card_summary(idx["demowillow"], self.th)
        self.assertEqual(hm.display_name(summary), "DemoWillow  [Demo]")
        prof = hm.entry_from_profile("DemoFlint", {}, True)
        self.assertEqual(hm.display_name(hm.card_summary(prof, self.th)),
                         "DemoFlint  [Demo]")

    def test_profile_entry_for_players_outside_boards(self):
        entry = hm.entry_from_profile("far", {"mining": 7 * MICRO})
        self.assertEqual(entry["xp"]["mining"], 7 * MICRO)
        self.assertEqual(entry["xp"]["cooking"], 0)
        self.assertEqual(entry["ranks"], {})
        summary = hm.card_summary(entry, self.th)
        self.assertIsNone(summary["overall_rank"])
        self.assertEqual(summary["total_level"], 6)

    def test_rows_with_levels(self):
        idx = hm.player_index(self._boards())
        boards = self._boards()
        mining = hm.rows_with_levels("mining", boards["mining"], idx, self.th)
        self.assertEqual(mining[0]["level"],
                         hm.level_of(10300 * MICRO, self.th))
        overall = hm.rows_with_levels("overall", boards["overall"], idx, self.th)
        self.assertEqual(overall[0]["level"],
                         hm.total_level(idx["yae"]["xp"], self.th))
        self.assertEqual(hm.level_label("overall", 7), "Total 7")
        self.assertEqual(hm.level_label("mining", 7), "Lv 7")
        self.assertEqual(hm.level_label("mining", None), "—")


def lrow(name, level, whole_xp, rank=0):
    return {"rank": rank, "username": name, "level": level,
            "xp": str(whole_xp * MICRO), "xp_display": str(whole_xp),
            "is_demo": False}


class TestLevelSort(unittest.TestCase):
    def test_level_beats_xp(self):
        # Spike has far more XP but Spread has the higher total level.
        rows = hm.rerank_by_level([lrow("Spike", 40, 90000),
                                   lrow("Spread", 60, 20000)])
        self.assertEqual([r["username"] for r in rows], ["Spread", "Spike"])
        self.assertEqual([r["rank"] for r in rows], [1, 2])

    def test_equal_total_level_is_broken_by_xp(self):
        rows = hm.rerank_by_level([lrow("low", 50, 1000),
                                   lrow("high", 50, 5000),
                                   lrow("mid", 50, 3000)])
        self.assertEqual([r["username"] for r in rows],
                         ["high", "mid", "low"])
        self.assertEqual([r["rank"] for r in rows], [1, 2, 3])

    def test_only_level_and_xp_both_equal_share_a_rank(self):
        rows = hm.rerank_by_level([lrow("a", 50, 1000), lrow("b", 50, 1000),
                                   lrow("c", 50, 900), lrow("d", 40, 5000)])
        self.assertEqual([(r["username"], r["rank"]) for r in rows],
                         [("a", 1), ("b", 1), ("c", 3), ("d", 4)])

    def test_unknown_level_means_no_level_ranking(self):
        rows = [lrow("a", 50, 1), dict(lrow("b", 0, 1), level=None)]
        self.assertIsNone(hm.rerank_by_level(rows))
        self.assertEqual(hm.rerank_by_level([]), [])

    def test_input_is_not_mutated(self):
        src = [lrow("a", 10, 1, rank=9)]
        hm.rerank_by_level(src)
        self.assertEqual(src[0]["rank"], 9)

    def test_standing_gap_in_levels_when_target_is_higher(self):
        rows = hm.rerank_by_level([lrow("lead", 60, 9000),
                                   lrow("me", 55, 20000)])
        st = hm.standing_by_level(rows, "me")
        self.assertEqual((st["rank"], st["target"], st["gap_levels"]),
                         (2, "lead", 5))
        text = hm.standing_text(st, "overall")
        self.assertEqual(text["goal"], "5 more total levels to reach lead")
        one = hm.standing_by_level(hm.rerank_by_level(
            [lrow("lead", 56, 9000), lrow("me", 55, 20000)]), "me")
        self.assertEqual(hm.standing_text(one, "overall")["goal"],
                         "1 more total level to reach lead")

    def test_standing_gap_in_xp_when_level_is_the_same(self):
        rows = hm.rerank_by_level([lrow("lead", 55, 9000),
                                   lrow("me", 55, 4000)])
        st = hm.standing_by_level(rows, "me")
        self.assertEqual(st["gap_levels"], 0)
        self.assertEqual(hm.standing_text(st, "overall")["goal"],
                         "5,000 XP to pass lead (same total level)")

    def test_leader_and_absent_and_untrained(self):
        rows = hm.rerank_by_level([lrow("lead", 60, 9000),
                                   lrow("me", 50, 100),
                                   lrow("idle", 6, 0)])
        self.assertTrue(hm.standing_by_level(rows, "lead")["leader"])
        self.assertIsNone(hm.standing_by_level(rows, "ghost"))
        idle = hm.standing_by_level(rows, "idle")
        self.assertFalse(idle["trained"])
        self.assertEqual(hm.standing_text(idle, "overall")["headline"],
                         "Not ranked yet.")

    def test_xp_mode_text_is_unchanged(self):
        rows = board([("a", 100), ("me", 40)])
        text = hm.standing_text(hm.standing(rows, "me"), "overall")
        self.assertEqual(text["goal"], "60 XP to pass a")


if __name__ == "__main__":
    unittest.main()
