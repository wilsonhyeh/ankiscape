# tests/test_icon_cache.py - Bounded icon cache and reviewed icon slots.
"""Falsifies the rendering contract: LRU bound by entries and estimated
bytes, misses/fallbacks cached, revision/DPI keying, slot map coverage and
achievement icons never falling back to an unrelated cooking icon."""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evolved import assets  # noqa: E402
from evolved.icons import fallback_path  # noqa: E402


class _FakePixmap:
    def __init__(self, width=32, height=32):
        self._w = width
        self._h = height

    def width(self):
        return self._w

    def height(self):
        return self._h


def _load_widgets_cache():
    """Import evolved.ui.widgets headlessly (Qt stubs when aqt is absent)."""
    import importlib

    try:
        return importlib.import_module("evolved.ui.widgets")
    except Exception:
        return None


WIDGETS = _load_widgets_cache()


class IconCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if WIDGETS is None:
            raise unittest.SkipTest("widgets module not importable headlessly")
        cls.Cache = WIDGETS.IconCache

    def test_lru_eviction_by_entries(self):
        cache = self.Cache(max_entries=3, max_bytes=10 ** 9,
                           decode=lambda key: _FakePixmap())
        for i in range(3):
            cache.get(("k", i))
        self.assertEqual(len(cache), 3)
        cache.get(("k", 0))  # touch: key 0 becomes most recent
        cache.get(("k", 3))
        self.assertEqual(len(cache), 3)
        self.assertIn(("k", 0), cache._data)
        self.assertNotIn(("k", 1), cache._data)
        self.assertGreaterEqual(cache.stats["evictions"], 1)

    def test_byte_budget_eviction(self):
        # 10x10x4 = 400 bytes each; budget fits two.
        cache = self.Cache(max_entries=256, max_bytes=1000,
                           decode=lambda key: _FakePixmap(10, 10))
        for i in range(4):
            cache.get(("k", i))
        self.assertLessEqual(len(cache), 2)
        self.assertLessEqual(cache.bytes_used(), 1000)

    def test_oversized_value_is_not_cached(self):
        cache = self.Cache(max_entries=8, max_bytes=1000,
                           decode=lambda key: _FakePixmap(100, 100))
        value = cache.get(("big",))
        self.assertIsNotNone(value)
        self.assertEqual(len(cache), 0)

    def test_misses_and_fallbacks_are_cached(self):
        calls = {"n": 0}

        def decode(key):
            calls["n"] += 1
            return None

        cache = self.Cache(max_entries=8, max_bytes=1000, decode=decode)
        self.assertIsNone(cache.get(("missing",)))
        self.assertIsNone(cache.get(("missing",)))
        self.assertEqual(calls["n"], 1)
        self.assertEqual(cache.stats["fallbacks"], 1)
        self.assertEqual(cache.stats["hits"], 1)

    def test_decode_errors_do_not_escape(self):
        def decode(key):
            raise RuntimeError("boom")

        cache = self.Cache(max_entries=8, max_bytes=1000, decode=decode)
        self.assertIsNone(cache.get(("bad",)))
        self.assertEqual(cache.stats["errors"], 1)

    def test_clear_and_stats_shape(self):
        cache = self.Cache(max_entries=8, max_bytes=1000,
                           decode=lambda key: _FakePixmap())
        cache.get(("k",))
        cache.clear()
        self.assertEqual(len(cache), 0)
        self.assertEqual(cache.bytes_used(), 0)

    def test_dpi_and_scale_key_separation(self):
        keys = []

        def decode(key):
            keys.append(key)
            return _FakePixmap()

        cache = self.Cache(max_entries=16, max_bytes=10 ** 9, decode=decode)
        cache.get(("p", "rev1", 48, 1.0))
        cache.get(("p", "rev1", 48, 2.0))
        cache.get(("p", "rev2", 48, 1.0))
        cache.get(("p", "rev1", 64, 1.0))
        self.assertEqual(len(keys), 4)

    def test_live_cache_is_bounded(self):
        from evolved.ui import widgets as live
        stats = live.icon_cache_stats()
        self.assertLessEqual(stats["max_entries"], 256)
        self.assertLessEqual(stats["max_bytes"], 16 * 1024 * 1024)


class SlotMapTests(unittest.TestCase):
    def test_every_static_slot_resolves_to_a_bundled_file(self):
        for slot, rel in assets.SLOT_ICONS.items():
            path = assets.slot_icon_path(slot)
            self.assertTrue(os.path.isfile(path), f"{slot} -> {path}")
            self.assertTrue(path.endswith(rel.replace("/", os.sep)),
                            f"{slot} resolved {path}, not {rel}")

    def test_dynamic_slots_resolve_display_and_skill_art(self):
        self.assertEqual(assets.slot_icon_path("training.result",
                                               display="Rune essence"),
                         assets.display_icon("Rune essence"))
        self.assertEqual(assets.slot_icon_path("training.gather",
                                               skill="mining"),
                         assets.skill_icon_path("mining"))
        self.assertEqual(assets.slot_icon_path("bank.item",
                                               display="Gold ring"),
                         assets.display_icon("Gold ring"))

    def test_unknown_slot_falls_back_without_crashing(self):
        path = assets.slot_icon_path("no.such.slot")
        self.assertTrue(os.path.isfile(path))

    def test_achievement_icons_are_explicit(self):
        self.assertTrue(assets.achievement_icon_path("skill_10_mining")
                        .endswith(os.path.join("icon", "mining_icon.png")))
        self.assertTrue(assets.achievement_icon_path("first_catch")
                        .endswith(os.path.join("icon", "fishing_icon.png")))
        for aid in ("first_cook", "cooks_100", "cooks_1000"):
            self.assertTrue(assets.achievement_icon_path(aid)
                            .endswith(os.path.join("icon", "cooking_icon.png")))

    def test_unknown_achievement_has_no_misleading_icon(self):
        self.assertIsNone(assets.achievement_icon_path("mystery_feat"))
        self.assertIsNone(assets.achievement_icon_path("skill_bad"))

    def test_manifest_revision_is_stable_and_nonempty(self):
        first = assets.manifest_revision()
        second = assets.manifest_revision()
        self.assertEqual(first, second)
        self.assertNotEqual(first, "unknown")
        self.assertGreaterEqual(len(first), 10)

    def test_slots_are_recorded_in_manifest(self):
        import json

        with open(os.path.join(ROOT, "assets", "manifest.json"),
                  encoding="utf-8") as fh:
            manifest = json.load(fh)
        paths = {r.get("path") for r in manifest["files"]}
        for slot, rel in assets.SLOT_ICONS.items():
            self.assertIn(rel, paths, f"{slot} missing manifest record")


if __name__ == "__main__":
    unittest.main()
