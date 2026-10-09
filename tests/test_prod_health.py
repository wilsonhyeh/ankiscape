import datetime as dt
import importlib.util
import os
import unittest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "dev", "prod_health.py")
_spec = importlib.util.spec_from_file_location("prod_health", _PATH)
prod_health = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prod_health)

NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone.utc)
HEALTHY = {"db_bytes": 41 * 1024 * 1024, "live_ops": 22780, "largest_game_ops": 4495,
           "games": 24, "stale_folds": 0, "oldest_stale_since": None,
           "missing_folds": 0, "cron_failures_24h": 0}


class ProdHealthThresholds(unittest.TestCase):
    def test_healthy_production_passes(self):
        self.assertEqual(prod_health.problems(HEALTHY, NOW), [])

    def test_each_threshold_fails_on_its_own(self):
        cases = {
            "db_bytes": 351 * 1024 * 1024,
            "missing_folds": 1,
            "largest_game_ops": 20_001,
            "cron_failures_24h": 2,
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                found = prod_health.problems(dict(HEALTHY, **{key: value}), NOW)
                self.assertEqual(len(found), 1, found)

    def test_stale_fold_fails_only_after_fifteen_minutes(self):
        fresh = dict(HEALTHY, stale_folds=1, oldest_stale_since="2026-10-08T11:50:00+00:00")
        old = dict(HEALTHY, stale_folds=1, oldest_stale_since="2026-10-08T11:40:00Z")
        self.assertEqual(prod_health.problems(fresh, NOW), [])
        self.assertEqual(len(prod_health.problems(old, NOW)), 1)


if __name__ == "__main__":
    unittest.main()
