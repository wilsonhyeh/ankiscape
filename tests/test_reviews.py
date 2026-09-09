import unittest

from evolved import reviews


class TestReviews(unittest.TestCase):
    def test_eligibility_matrix(self):
        act = 1000
        self.assertTrue(reviews.is_eligible(ease=3, revlog_type=1, review_ts=1000, activated_at=act))
        self.assertTrue(reviews.is_eligible(ease=2, revlog_type=0, review_ts=1500, activated_at=act))
        self.assertTrue(reviews.is_eligible(ease=4, revlog_type=3, review_ts=1500, activated_at=act))
        # Rating 1 never awards.
        self.assertFalse(reviews.is_eligible(ease=1, revlog_type=1, review_ts=1500, activated_at=act))
        # Manual reschedule / preview / cancelled never award.
        self.assertFalse(reviews.is_eligible(ease=0, revlog_type=1, review_ts=1500, activated_at=act))
        self.assertFalse(reviews.is_eligible(ease=3, revlog_type=1, review_ts=1500, activated_at=act, preview=True))
        self.assertFalse(reviews.is_eligible(ease=3, revlog_type=1, review_ts=1500, activated_at=act, cancelled=True))
        # Pre-activation history does not count.
        self.assertFalse(reviews.is_eligible(ease=3, revlog_type=1, review_ts=999, activated_at=act))

    def test_review_key_deterministic_ascii(self):
        k1 = reviews.make_review_key("game-1", 42, 7)
        k2 = reviews.make_review_key("game-1", 42, 7)
        self.assertEqual(k1, k2)
        self.assertRegex(k1, r"^[0-9a-f]+$")
        self.assertNotEqual(k1, reviews.make_review_key("game-2", 42, 7))

    def test_uncredited_scan_handles_late_arrival(self):
        act = 1000
        history = [
            {"revlog_id": 10, "card_id": 1, "ease": 3, "revlog_type": 1, "ts": 1100, "review_key": "k10"},
            {"revlog_id": 30, "card_id": 2, "ease": 3, "revlog_type": 1, "ts": 1300, "review_key": "k30"},
            # Late arrival older than previous max id.
            {"revlog_id": 20, "card_id": 3, "ease": 3, "revlog_type": 1, "ts": 1200, "review_key": "k20"},
            {"revlog_id": 40, "card_id": 4, "ease": 1, "revlog_type": 1, "ts": 1400, "review_key": "k40"},
        ]
        out, stats = reviews.find_uncredited(history, {"k10"}, act)
        keys = [r["review_key"] for r in out]
        # k40 (rating 1) excluded; late k20 included despite arriving after k30.
        self.assertEqual(keys, ["k20", "k30"])
        self.assertEqual(stats["max_revlog_id"], 40)

    def test_fingerprint_excludes_sync_seq(self):
        f1 = reviews.immutable_fingerprint(revlog_id=5, card_id=6, ease=3, review_ts=9, revlog_type=1)
        f2 = reviews.immutable_fingerprint(revlog_id=5, card_id=6, ease=3, review_ts=9, revlog_type=1)
        self.assertEqual(f1, f2)


if __name__ == "__main__":
    unittest.main()
