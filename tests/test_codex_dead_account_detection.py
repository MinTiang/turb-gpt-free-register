# -*- coding: utf-8 -*-
import unittest

from core.openai_auth import detect_account_unusable_response_body
from core.browser_codex_oauth import _read_email_otp_validate_dead_code


class CodexDeadAccountDetectionTests(unittest.TestCase):
    def test_detect_account_unusable_response_body_uses_error_code(self):
        self.assertEqual(
            detect_account_unusable_response_body('{"error":{"code":"account_deactivated"}}'),
            "account_deactivated",
        )
        self.assertEqual(
            detect_account_unusable_response_body('{"error":{"code":"account_deleted"}}'),
            "account_deleted",
        )
        self.assertEqual(detect_account_unusable_response_body('Your account has been deactivated.'), "")

    def test_browser_email_submit_returns_deactivated_from_response_tracker(self):
        class Driver:
            def __init__(self, rows):
                self.rows = rows

            def execute_script(self, script, *args):
                return self.rows

        rows = [{
            "url": "https://auth.openai.com/api/accounts/email-otp/validate",
            "status": 400,
            "body": '{"error":{"code":"account_deactivated"}}',
        }]
        self.assertEqual(_read_email_otp_validate_dead_code(Driver(rows)), "account_deactivated")
        self.assertEqual(_read_email_otp_validate_dead_code(Driver([])), "")
        self.assertEqual(
            _read_email_otp_validate_dead_code(Driver([{"body": '{"ok":true}'}])),
            "",
        )


if __name__ == "__main__":
    unittest.main()
