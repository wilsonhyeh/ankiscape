import unittest

import mode
from runtime import ClassicAdapter, EvolvedAdapter, Runtime, reset_runtime_for_tests


class TestMode(unittest.TestCase):
    def test_normalize_unknown_is_classic(self):
        self.assertEqual(mode.normalize_requested(""), mode.CLASSIC)
        self.assertEqual(mode.normalize_requested(None), mode.CLASSIC)
        self.assertEqual(mode.normalize_requested("EVOLVED"), mode.EVOLVED)

    def test_chooser_semantics(self):
        self.assertEqual(mode.chooser_default(), mode.EVOLVED)
        # Closing/Escape selects Classic.
        self.assertEqual(mode.chooser_cancel_result(), mode.CLASSIC)

    def test_requested_persistence_roundtrip(self):
        store = {}

        def get(k, default=None):
            return store.get(k, default)

        def set_(k, v):
            store[k] = v

        self.assertEqual(mode.get_requested_mode(get), mode.CLASSIC)
        mode.set_requested_mode(set_, mode.EVOLVED)
        self.assertEqual(mode.get_requested_mode(get), mode.EVOLVED)


class TestRuntime(unittest.TestCase):
    def setUp(self):
        reset_runtime_for_tests()

    def test_one_adapter_per_profile_and_generation(self):
        from runtime import get_runtime

        rt = get_runtime()
        g1 = rt.begin_profile(mode.EVOLVED, EvolvedAdapter(game_uuid="g1"))
        self.assertTrue(rt.profile_loaded)
        self.assertEqual(rt.active_adapter.name, "evolved")
        # Requested change mid-process does not swap the active adapter.
        rt.requested_mode = mode.CLASSIC
        self.assertEqual(rt.active_adapter.name, "evolved")
        old = rt.end_profile()
        self.assertEqual(old, g1)
        # Generation invalidated before release.
        self.assertEqual(rt.generation, 0)
        self.assertFalse(rt.profile_loaded)

    def test_stale_background_result_rejected(self):
        rt = Runtime()
        adapter = EvolvedAdapter(game_uuid="g1")
        adapter.bind_user("u1")
        g = rt.begin_profile(mode.EVOLVED, adapter)
        self.assertTrue(rt.is_result_fresh(g, "u1"))
        self.assertFalse(rt.is_result_fresh(g, "u2"))
        self.assertFalse(rt.is_result_fresh(g + 999, "u1"))
        rt.end_profile()
        self.assertFalse(rt.is_result_fresh(g, "u1"))

    def test_no_profile_no_action(self):
        rt = Runtime()
        self.assertIsNone(rt.route_review_answer({}))

    def test_game_exception_does_not_raise(self):
        class Boom(ClassicAdapter):
            def on_review_answer(self, ctx):
                raise RuntimeError("boom")

        rt = Runtime()
        rt.begin_profile(mode.CLASSIC, Boom())
        out = rt.route_review_answer({})
        self.assertFalse(out["ok"])
        self.assertTrue(out["diagnostics"])


if __name__ == "__main__":
    unittest.main()
