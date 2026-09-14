import importlib.util
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _load(name, rel_path):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_ROOT, rel_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load("ankiscape_account_journey_runner",
               "dev/account_journey_e2e.py")


class TestTargetNormalization(unittest.TestCase):
    def test_anki_versions_match_matrix_naming(self):
        self.assertEqual(runner._normalize_anki("23.10.1"), "23.10")
        self.assertEqual(runner._normalize_anki("23.10"), "23.10")
        self.assertEqual(runner._normalize_anki("26.08.1"), "26.8.1")
        self.assertEqual(runner._normalize_anki("26.8.1"), "26.8.1")

    def test_qt_major(self):
        self.assertEqual(runner._normalize_qt("6.11.0"), "6")
        self.assertEqual(runner._normalize_qt("5.15.2"), "5")

    def test_target_key(self):
        self.assertEqual(
            runner.target_key({"os": "macos", "anki": "23.10",
                               "qt": "6"}),
            "macos-23.10-qt6")
        self.assertEqual(
            runner.target_key({"os": "Windows", "anki": "26.08.1",
                               "qt": "6.11"}),
            "windows-26.8.1-qt6")


class TestRecordValidation(unittest.TestCase):
    def _record(self, key, *, exit_code=0, failed=None, journey="",
                assertions=None):
        return {"target_key": key, "exit_code": exit_code,
                "failed": failed or [], "journey": journey,
                "assertions": assertions or {}}

    def test_all_targets_present_passes(self):
        required = ([
            {"os": "macos", "anki": "23.10", "qt": "6"},
            {"os": "linux", "anki": "26.8.1", "qt": "6"},
        ], [])
        records = {
            "macos-23.10-qt6": self._record("macos-23.10-qt6"),
            "linux-26.8.1-qt6": self._record("linux-26.8.1-qt6"),
        }
        self.assertEqual(runner._validate(records, required), [])

    def test_missing_and_failed_and_unexpected_report(self):
        required = ([
            {"os": "macos", "anki": "23.10", "qt": "6"},
        ], [])
        records = {
            "linux-26.8.1-qt6": self._record("linux-26.8.1-qt6"),
        }
        errors = runner._validate(records, required)
        self.assertIn("missing_target:macos-23.10-qt6", errors)
        self.assertIn("unexpected_target:linux-26.8.1-qt6", errors)

    def test_failed_journey_is_a_failure(self):
        required = ([{"os": "macos", "anki": "23.10", "qt": "6"}], [])
        records = {
            "macos-23.10-qt6": self._record(
                "macos-23.10-qt6", failed=["linkage_automatic"]),
        }
        errors = runner._validate(records, required)
        self.assertTrue(any(error.startswith("journey_failed:")
                            for error in errors), errors)

    def test_nonzero_exit_is_a_failure(self):
        required = ([{"os": "macos", "anki": "23.10", "qt": "6"}], [])
        records = {
            "macos-23.10-qt6": self._record("macos-23.10-qt6",
                                            exit_code=1),
        }
        errors = runner._validate(records, required)
        self.assertIn("journey_exit:macos-23.10-qt6", errors)

    def test_required_journey_assertions_must_pass(self):
        required = ([{"os": "macos", "anki": "23.10", "qt": "6"}], [
            {"target": {"os": "macos", "anki": "23.10", "qt": "6"},
             "journey": "ui-account-lifecycle",
             "assertions": ["resend_route", "delete_flow"]},
        ])
        records = {
            "macos-23.10-qt6": self._record(
                "macos-23.10-qt6", journey="ui-account-lifecycle",
                assertions={"resend_route": True, "delete_flow": False}),
        }
        errors = runner._validate(records, required)
        self.assertNotIn(
            "missing_assertion:macos-23.10-qt6:resend_route", errors)
        self.assertIn("failed_assertion:macos-23.10-qt6:delete_flow",
                      errors)

    def test_assertion_requirement_is_scoped_to_its_journey(self):
        required = ([{"os": "macos", "anki": "23.10", "qt": "6"}], [
            {"target": {"os": "macos", "anki": "23.10", "qt": "6"},
             "journey": "ui-test-leaderboard",
             "assertions": ["test_leaderboard_labeled"]},
        ])
        records = {
            "macos-23.10-qt6": self._record(
                "macos-23.10-qt6", journey="ui-account-lifecycle",
                assertions={"resend_route": True}),
        }
        self.assertEqual(runner._validate(records, required), [])


if __name__ == "__main__":
    unittest.main()
