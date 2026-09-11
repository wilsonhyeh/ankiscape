# tests/test_guide_credits.py - Concise credits and rights wording.
"""Falsifies the credits contract: the exact opening copy, ownership and
non-affiliation language, required license notices, offline details, no
internal audit dump on the normal page, and no false authorization claims."""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evolved.ui.guide import credits_page  # noqa: E402

OPENING = ("AnkiScape is an independent, noncommercial passion project made "
           "out of love for Old School RuneScape. It brings a little of that "
           "joy to studying with Anki; it is not intended to replace "
           "RuneScape or profit from it.")


class CreditsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = credits_page({})

    def test_opening_copy_is_exact(self):
        self.assertIn(OPENING, self.html)

    def test_ownership_and_non_affiliation_present(self):
        lowered = self.html.lower()
        self.assertIn("belong to jagex ltd", lowered)
        self.assertIn("not affiliated with or endorsed by jagex", lowered)

    def test_wiki_attribution_and_license_notices(self):
        self.assertIn("Old School RuneScape Wiki", self.html)
        self.assertIn("SIL Open Font License 1.1", self.html)
        self.assertIn("fonts/OFL.txt", self.html)

    def test_no_false_authorization_claims(self):
        lowered = self.html.lower()
        for forbidden in ("not copied", "fair use", "permission granted",
                          "licensed under", "used with permission"):
            self.assertNotIn(forbidden, lowered)

    def test_normal_page_has_no_internal_release_notes(self):
        self.assertNotIn("Release note", self.html)
        self.assertNotIn("release_issue", self.html)

    def test_asset_details_are_offline_and_complete(self):
        # Sources and hashes stay available in the details section.
        self.assertIn("sha256", self.html)
        self.assertIn("oldschool.runescape.wiki", self.html)
        self.assertNotIn("http://", self.html)

    def test_rights_doc_records_policy_references(self):
        path = os.path.join(ROOT, "docs", "ASSET-RIGHTS.md")
        with open(path, encoding="utf-8") as fh:
            doc = fh.read()
        self.assertIn("legal.jagex.com/docs/policies/fan-content-policy", doc)
        self.assertIn("6.1.3", doc)
        self.assertIn("8.1", doc)
        self.assertIn("14.1", doc)
        self.assertIn("not cleared", doc)


if __name__ == "__main__":
    unittest.main()
