import json
import os
import tempfile
import unittest

from evolved import hiscores_seen as hs


class TestHiscoresSeen(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_round_trip(self):
        data = {"mining": {"ts": 123.5, "ranks": {"yae": 1, "amy": 2}}}
        self.assertTrue(hs.save(self.dir, data))
        self.assertEqual(hs.load(self.dir), data)

    def test_missing_file_is_no_memory(self):
        self.assertEqual(hs.load(self.dir), {})
        self.assertEqual(hs.load(os.path.join(self.dir, "nope")), {})

    def test_corrupt_file_is_no_memory(self):
        with open(hs.path_for(self.dir), "w") as fh:
            fh.write("{not json")
        self.assertEqual(hs.load(self.dir), {})

    def test_wrong_version_is_ignored(self):
        with open(hs.path_for(self.dir), "w") as fh:
            json.dump({"version": 99, "boards": {
                "mining": {"ts": 1, "ranks": {"a": 1}}}}, fh)
        self.assertEqual(hs.load(self.dir), {})

    def test_bad_entries_are_dropped_good_kept(self):
        with open(hs.path_for(self.dir), "w") as fh:
            json.dump({"version": 1, "boards": {
                "mining": {"ts": 5, "ranks": {"a": 1, "b": "x", "c": 0,
                                              "d": True, "e": 3}},
                "fishing": {"ts": "bad", "ranks": {"a": 1}},
                "cooking": {"ts": 1, "ranks": "nope"},
                "smithing": "junk"}}, fh)
        self.assertEqual(hs.load(self.dir),
                         {"mining": {"ts": 5.0, "ranks": {"a": 1, "e": 3}}})

    def test_bounded_sizes(self):
        big = {"mining": {"ts": 1, "ranks": {f"p{i}": i + 1
                                             for i in range(hs.MAX_PLAYERS + 50)}}}
        self.assertTrue(hs.save(self.dir, big))
        self.assertEqual(len(hs.load(self.dir)["mining"]["ranks"]),
                         hs.MAX_PLAYERS)

    def test_failed_save_keeps_old_file_and_leaves_no_temp(self):
        hs.save(self.dir, {"mining": {"ts": 1, "ranks": {"a": 1}}})
        before = hs.load(self.dir)
        self.assertFalse(hs.save(os.path.join(self.dir, "missing-dir"),
                                 {"mining": {"ts": 2, "ranks": {"a": 2}}}))
        self.assertEqual(hs.load(self.dir), before)
        self.assertEqual([n for n in os.listdir(self.dir)
                          if n.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()
