# -*- coding: utf-8 -*-
"""codex2api 推送: 两种授权的参数区分与请求形态。"""
import json
import unittest
from unittest.mock import MagicMock, patch

from core import codex2api_push as push


class ClientIdResolutionTests(unittest.TestCase):
    def test_driver_mapping(self):
        # 平台授权走官网 client；接码(cloak/browse 同属接码链路)走 Codex CLI
        self.assertEqual(push.resolve_client_id("platform"), push.PLATFORM_CLIENT_ID)
        self.assertEqual(push.resolve_client_id("protocol"), push.CLI_CLIENT_ID)
        self.assertEqual(push.resolve_client_id("cloak"), push.CLI_CLIENT_ID)

    def test_payload_client_id_used_when_driver_unknown(self):
        payload = {"client_id": push.PLATFORM_CLIENT_ID}
        self.assertEqual(push.resolve_client_id("", payload), push.PLATFORM_CLIENT_ID)

    def test_detect_from_id_token_claims(self):
        import base64

        def mk(payload):
            seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
            return f"h.{seg}.s"

        self.assertEqual(
            push.resolve_client_id("", {"id_token": mk({"aud": [push.PLATFORM_CLIENT_ID]})}),
            push.PLATFORM_CLIENT_ID,
        )
        self.assertEqual(
            push.resolve_client_id("", {"id_token": mk({"azp": push.CLI_CLIENT_ID})}),
            push.CLI_CLIENT_ID,
        )

    def test_unknown_driver_without_hint_returns_empty(self):
        self.assertEqual(push.resolve_client_id("unknown", {}), "")


class BuildImportEntryTests(unittest.TestCase):
    def test_platform_entry_carries_platform_client(self):
        entry = push.build_import_entry(
            {"email": "u@example.com", "account_id": "a1", "access_token": "at",
             "refresh_token": "rt", "id_token": "idt", "expired": "2026-10-01T00:00:00Z"},
            "platform",
        )
        self.assertEqual(entry["client_id"], push.PLATFORM_CLIENT_ID)
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
        call, _ = self._push("platform", {
            "email": "u@example.com", "access_token": "at", "refresh_token": "rt"})
        self.assertTrue(call["url"].endswith("/api/admin/accounts/import"))
        self.assertEqual(call["data"], {"format": "json"})
        fname, blob, ctype = call["files"]["file"]
        self.assertTrue(fname.startswith("codex-") and fname.endswith(".json"))
        self.assertEqual(ctype, "application/json")
        sent = json.loads(blob.decode("utf-8"))
        self.assertEqual(sent["client_id"], push.PLATFORM_CLIENT_ID)
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
