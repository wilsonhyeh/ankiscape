import os
import unittest

from evolved.icons import fallback_path, placeholder_bytes, resolve_icon


class TestIcons(unittest.TestCase):
    def test_fallback_is_shipped_and_deterministic(self):
        self.assertEqual(placeholder_bytes(), placeholder_bytes())
        self.assertTrue(len(placeholder_bytes()) > 0)
        self.assertTrue(os.path.isfile(fallback_path()))

    def test_missing_asset_never_breaks_and_never_writes(self):
        icon_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "icon")
        before = sorted(os.listdir(icon_dir))
        path = resolve_icon(kind="fish", display="No Such Fish Ever",
                            mapping={})
        self.assertEqual(path, fallback_path())
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), placeholder_bytes())
        # No per-item placeholder was generated at runtime.
        self.assertEqual(sorted(os.listdir(icon_dir)), before)
        self.assertFalse(os.path.isdir(os.path.join(icon_dir, "placeholder")))


if __name__ == "__main__":
    unittest.main()
