# tests/test_diagnostics.py - Diagnostics allowlist and issue-URL contract.
"""Falsifies the privacy contract: secrets and personal data cannot reach a
report, the ring stays bounded, preview text equals copied/opened content,
oversized reports fall back to the blank template with the body retained, and
Unicode round-trips."""
from __future__ import annotations

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evolved import diagnostics as diag  # noqa: E402

SECRETS = [
    "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig",
    "sb_secret_abcdef1234567890",
    "user@example.com",
    "hostname.local",
    "/Users/alice/Library/Application Support/Anki2/User 1/collection.anki2",
    "192.168.1.25",
    "password=hunter2; token=abc123",
    "deck: Step 2 CK::Card 42",
]


class AllowlistTests(unittest.TestCase):
    def test_forbidden_material_cannot_enter_a_payload(self):
        payload = diag.collect(
            addon_version="3.0.0 <script>",
            artifact_id="artifact; rm -rf /",
            anki="26.8.1",
            os_name="macOS (Wilson's MacBook Pro)",
            mode="evolved<script>",
            error_code="persist_failed: " + SECRETS[0],
            pending=7,
            toggles={"hud_visible": True, "token": "abc123",
                     "password": "hunter2", "unknown": True},
            timings={"accepted_answer_hook_p95": 12.3,
                     "raw_trace": SECRETS[2]})
        text = diag.render_text(payload)
        for secret in SECRETS:
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, str(payload))
        self.assertNotIn("token", payload["toggles"])
        self.assertNotIn("password", payload["toggles"])
        self.assertNotIn("unknown", payload["toggles"])
        self.assertNotIn("raw_trace", payload["timings"])
        self.assertNotIn("<script>", text)
        self.assertEqual(payload["pending_bucket"], "5-19")
        self.assertEqual(payload["mode"], "unknown")

    def test_allowlisted_fields_render_deterministically(self):
        payload = diag.collect(addon_version="3.0.0", artifact_id="abc123",
                               anki="26.8.1", python="3.13.2", qt="6.7.2",
                               os_name="macos", arch="arm64", mode="evolved",
                               error_code="persist_failed", pending=0,
                               toggles={"sound": True},
                               timings={"reward_completion_p95": 108.0})
        text = diag.render_text(payload)
        self.assertEqual(text, diag.render_text(payload))
        self.assertIn("addon: 3.0.0 (artifact abc123)", text)
        self.assertIn("pending: 0", text)
        self.assertIn("sound=on", text)
        self.assertIn("reward_completion_p95=<=250ms", text)

    def test_pending_buckets_are_coarse(self):
        cases = {0: "0", 1: "1-4", 5: "5-19", 20: "20-99", 250: "100+"}
        for count, label in cases.items():
            self.assertEqual(
                diag.collect(pending=count)["pending_bucket"], label)

    def test_traceback_normalization_drops_paths_and_message(self):
        raw = ('Traceback (most recent call last):\n'
               '  File "/Users/alice/ankiscape/evolved/engine.py", line 42,'
               ' in credit_direct\n'
               '    token = "secret-token"\n'
               'ValueError: user@example.com failed\n')
        frames = diag.normalize_traceback(raw)
        self.assertEqual(frames,
                         ["ankiscape.evolved.engine:42:credit_direct"])
        self.assertNotIn("secret", " ".join(frames))

    def test_record_body_contains_no_raw_exception_text(self):
        body = diag.report_body(
            summary="Crash on review", happened="A dialog appeared",
            expected="No dialog", steps="1. Review",
            diagnostics_text=diag.render_text(diag.collect(error_code="x")))
        self.assertNotIn("Traceback", body)


class RingTests(unittest.TestCase):
    def test_ring_is_bounded(self):
        ring = diag.DiagnosticRing(max_bytes=2000)
        for i in range(200):
            ring.append(diag.collect(addon_version="3.0.0", pending=i))
        self.assertLessEqual(ring.size_bytes(), 2000)
        self.assertGreater(len(ring.entries()), 0)

    def test_ring_never_raises_on_bad_input(self):
        ring = diag.DiagnosticRing(max_bytes=1000)
        ring.append(None)  # type: ignore[arg-type]
        ring.append({"bad": object()})
        self.assertIsInstance(ring.serialized(), str)


class IssueUrlTests(unittest.TestCase):
    def test_small_report_prefills_fields(self):
        url, body = diag.issue_url(title="Crash on review",
                                   body="## Summary\nCrash")
        self.assertIsNotNone(url)
        self.assertIn("github.com/wilsonhyeh/ankiscape/issues/new", url)
        self.assertIn("template=bug_report.yml", url)
        self.assertIn("title=Crash", url)
        self.assertLessEqual(len(url.encode()), diag.URL_MAX_BYTES)
        self.assertEqual(body, "## Summary\nCrash")

    def test_oversized_report_falls_back_to_blank_template(self):
        url, body = diag.issue_url(title="Big", body="x" * 5000)
        self.assertIsNone(url)
        self.assertEqual(len(body), 5000)  # full body retained for copying

    def test_unicode_round_trips(self):
        text = "Café ☕ 日本語 — “quoted”"
        url, body = diag.issue_url(title=text, body=text)
        self.assertIsNotNone(url)
        self.assertIn("%E2%98%95", url)  # ☕ percent-encoded
        self.assertEqual(body, text)

    def test_url_encodes_everything_and_adds_no_secrets(self):
        # Percent-encoding keeps the URL valid; the real privacy boundary is
        # that diagnostics never carry secrets in the first place.
        url, _body = diag.issue_url(title="t", body="line one\nline two")
        self.assertNotIn(" ", url)
        self.assertNotIn("\n", url)
        safe_body = diag.report_body(
            summary="s", happened="h", expected="e", steps="1",
            diagnostics_text=diag.render_text(diag.collect(
                error_code="persist_failed", pending=3)))
        url2, body2 = diag.issue_url(title="t", body=safe_body)
        self.assertIsNotNone(url2)
        for secret in SECRETS:
            self.assertNotIn(secret, body2)


if __name__ == "__main__":
    unittest.main()
