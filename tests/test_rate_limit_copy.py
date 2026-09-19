# tests/test_rate_limit_copy.py - S6/R16: email-limit copy on the coded path.
"""R16/S6: the known-code path (`over_email_send_rate_limit`, routed through
`classify_error`) must carry the email distinction at every Retry-After value.
The `kind == "rate_limited"` fallback never sees an email-limit code by
construction, so this suite asserts the copy function directly and never claims
the fallback carries the distinction.
"""
import unittest

from evolved.accounts import RATE_LIMIT_UNKNOWN_COPY, rate_limit_copy


class TestRateLimitCopyEmailDistinction(unittest.TestCase):
    def test_email_limit_carries_email_copy_at_every_retry_after(self):
        for seconds in (0, 45, 120, 300):
            text = rate_limit_copy(seconds, email_limit=True)
            self.assertIn("Email limit", text, msg=f"seconds={seconds}")
            self.assertIn("Check spam", text, msg=f"seconds={seconds}")

    def test_non_email_stays_generic_at_every_retry_after(self):
        for seconds in (0, 45, 120, 300):
            text = rate_limit_copy(seconds, email_limit=False)
            self.assertNotIn("Email", text, msg=f"seconds={seconds}")
            self.assertNotIn("Check spam", text, msg=f"seconds={seconds}")

    def test_zero_retry_after_names_the_email_limit_without_a_reset_time(self):
        text = rate_limit_copy(0, email_limit=True)
        self.assertIn("Email limit reached", text)
        self.assertNotIn("about", text)

    def test_unknown_retry_after_stays_email_specific_without_a_time(self):
        for value in (None, -5, "not-a-number"):
            text = rate_limit_copy(value, email_limit=True)
            self.assertIn("Email limit reached", text, msg=f"value={value!r}")
            self.assertNotIn("about", text, msg=f"value={value!r}")

    def test_generic_unknown_copy_is_unchanged(self):
        self.assertEqual(rate_limit_copy(0), RATE_LIMIT_UNKNOWN_COPY)
        self.assertEqual(rate_limit_copy(None), RATE_LIMIT_UNKNOWN_COPY)

    def test_minute_pluralization(self):
        self.assertIn("1 minute.", rate_limit_copy(45, email_limit=True))
        self.assertIn("2 minutes.", rate_limit_copy(120, email_limit=True))
        self.assertIn("5 minutes.", rate_limit_copy(300, email_limit=True))


if __name__ == "__main__":
    unittest.main()
