# -*- coding: utf-8 -*-
"""codex2api 推送: 两种授权的参数区分与请求形态。"""
import json
import unittest
from unittest.mock import MagicMock, patch

from core import codex2api_push as push


class ClientIdResolutionTests(unittest.TestCase):
    def test_always_cli_client(self):
        # 现仅剩接码授权来源, 恒为 Codex CLI client
        for driver in ("protocol", "cloak", "", "anything"):
            self.assertEqual(push.resolve_client_id(driver), push.CLI_CLIENT_ID)


class BuildImportEntryTests(unittest.TestCase):
    def test_entry_carries_cli_client(self):
        entry = push.build_import_entry(
            {"email": "u@example.com", "account_id": "a1", "access_token": "at",
             "refresh_token": "rt", "id_token": "idt", "expired": "2026-10-01T00:00:00Z"},
            "platform",
        )
        self.assertEqual(entry["client_id"], push.CLI_CLIENT_ID)
        self.assertEqual(entry["expires_at"], "2026-10-01T00:00:00Z")
        self.assertEqual(entry["email"], "u@example.com")

    def test_cli_entry_carries_cli_client(self):
        entry = push.build_import_entry(
            {"email": "u@example.com", "access_token": "at", "refresh_token": "rt"},
            "protocol",
        )
        self.assertEqual(entry["client_id"], push.CLI_CLIENT_ID)

    def test_empty_fields_dropped(self):
        entry = push.build_import_entry({"email": "u@example.com", "refresh_token": "rt"}, "protocol")
        self.assertNotIn("access_token", entry)
        self.assertNotIn("id_token", entry)


class PushRequestTests(unittest.TestCase):
    def _push(self, driver, payload, status=200, body='{"ok":true}'):
        calls = []

        def fake_post(url, headers=None, data=None, files=None, timeout=None):
            calls.append({"url": url, "headers": headers, "data": data, "files": files})
            return MagicMock(status_code=status, text=body)

        with patch("config.codex.CODEX2API_URL", "http://127.0.0.1:8099"), \
             patch("config.codex.CODEX2API_ADMIN_KEY", "k"), \
             patch.object(push.requests, "post", side_effect=fake_post):
            result = push.push_to_codex2api(payload, driver)
        return calls[0], result

    def test_multipart_form_with_format_json(self):
        # /accounts/import 是 multipart 上传（format=json + file 字段）
        call, _ = self._push("protocol", {
            "email": "u@example.com", "access_token": "at", "refresh_token": "rt"})
        self.assertTrue(call["url"].endswith("/api/admin/accounts/import"))
        self.assertEqual(call["data"], {"format": "json"})
        fname, blob, ctype = call["files"]["file"]
        self.assertTrue(fname.startswith("codex-") and fname.endswith(".json"))
        self.assertEqual(ctype, "application/json")
        sent = json.loads(blob.decode("utf-8"))
        self.assertEqual(sent["client_id"], push.CLI_CLIENT_ID)
        self.assertEqual(call["headers"]["X-Admin-Key"], "k")

    def test_rejects_payload_without_credentials(self):
        with patch("config.codex.CODEX2API_URL", "http://127.0.0.1:8099"), \
             patch("config.codex.CODEX2API_ADMIN_KEY", "k"):
            with self.assertRaises(RuntimeError):
                push.push_to_codex2api({"email": "u@example.com"}, "platform")

    def test_non_2xx_raises(self):
        with self.assertRaises(RuntimeError):
            self._push("protocol", {"email": "u@example.com", "refresh_token": "rt"},
                       status=400, body='{"error":"bad"}')


if __name__ == "__main__":
    unittest.main()
