import os
import unittest

from evolved.icons import placeholder_bytes, resolve_icon


class TestIcons(unittest.TestCase):
    def test_placeholder_deterministic(self):
        self.assertEqual(placeholder_bytes(), placeholder_bytes())
        self.assertTrue(len(placeholder_bytes()) > 0)

    def test_missing_asset_never_breaks(self):
        path = resolve_icon(kind="fish", display="No Such Fish Ever", mapping={})
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), placeholder_bytes())
        # Keep the tree clean: the placeholder probe must not ship.
        try:
            os.remove(path)
        except OSError:
            pass


if __name__ == "__main__":
    unittest.main()
