# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from core import email_provider
from core import page_ops


class DelayedEmailAllocationTests(unittest.TestCase):
    @patch("core.email_provider.acquire_email")
    def test_acquire_email_after_input_keeps_fixed_email_without_allocating(self, acquire):
        with patch.object(email_config, "USE_EMAIL_SERVICE", False):
            self.assertEqual(
                email_provider.acquire_email_after_input("fixed@example.com"),
                "fixed@example.com",
            )
        acquire.assert_not_called()

    @patch("core.email_provider.acquire_email", return_value="allocated@example.com")
    def test_acquire_email_after_input_allocates_only_for_automatic_mode(self, acquire):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            self.assertEqual(
                email_provider.acquire_email_after_input(None),
                "allocated@example.com",
            )
        acquire.assert_called_once_with()

    def test_browser_finds_input_before_allocating_email(self):
        """共享页面操作层(cloak 复用):必须先找到邮箱输入框再领邮箱。"""
        events = []
        email_input = object()

        def find_input(*args, **kwargs):
            events.append("find_input")
            return email_input

        def acquire():
            events.append("acquire_email")
            return "browser@example.com"

        with patch.object(page_ops, "_solve_cloudflare_challenge_if_present", return_value=False), \
                patch.object(page_ops, "_wait_for_email_input", side_effect=find_input), \
                patch.object(
                    page_ops,
                    "_human_type_text",
                    side_effect=lambda *args, **kwargs: events.append("type_email"),
                ), \
                patch.object(
                    page_ops,
                    "_email_input_value_state",
                    return_value={"inputs": [{"value": "browser@example.com"}]},
                ), \
                patch.object(
                    page_ops,
                    "_submit_email_step",
                    side_effect=lambda *args, **kwargs: events.append("submit_email"),
                ), \
                patch.object(page_ops, "_wait_email_submit_next_state", return_value="otp"), \
                patch.object(page_ops, "human_delay"), \
                patch.object(page_ops, "_check_manual_stop"):
            result = page_ops._submit_email_and_wait_next(
                object(), None, email_supplier=acquire
            )

        self.assertEqual(result, "otp")
        self.assertEqual(
            events,
            ["find_input", "acquire_email", "type_email", "submit_email"],
        )


if __name__ == "__main__":
    unittest.main()
