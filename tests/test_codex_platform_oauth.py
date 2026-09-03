# -*- coding: utf-8 -*-
"""
Platform OAuth 免接码 Codex 授权（core/codex_platform_oauth.py）单测。

全部 mock 网络层，不触真实 OpenAI / CPA。重点覆盖：
  1. authorize URL 参数集与 grok2api 完全对齐
  2. add-phone 降级提示切回接码驱动
  3. 凭证组装键集 / JWT exp-iat 时间戳 / 无 plan 后缀文件名
  4. CPA auth-files multipart 上传（URL/文件名/双 auth 头/重试）
  5. run_codex_oauth 驱动分发路由到 platform 模块
"""
import base64
import json
import unittest
from unittest.mock import MagicMock, patch

from core import codex_platform_oauth as pmod


def _make_jwt(claims: dict) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


class FakeResp:
    def __init__(self, status_code=200, text="", headers=None, url="", json_data=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.url = url
        self._json_data = json_data

    def json(self):
        if self._json_data is None:
            raise ValueError("no json")
        return self._json_data


def _fake_session():
    sess = MagicMock()
    sess.device_id = "00000000-0000-0000-0000-000000000001"
    sess.proxy = ""
    sess.browser_profile = {"user_agent": "Mozilla/5.0 Test"}
    sess.session = MagicMock()
    sess.session.impersonate = "chrome142"
    return sess


class PlatformAuthorizeUrlTests(unittest.TestCase):
    def test_authorize_url_params_match_grok2api(self):
        sess = _fake_session()
        captured = {}

        def fake_get(url, headers=None, allow_redirects=True):
            captured["url"] = url
            return FakeResp(status_code=200, url="https://auth.openai.com/log-in", json_data={"page": {"type": "login"}})

        sess.get = fake_get
        with patch.object(pmod.proto, "_generate_pkce", return_value=("verifier-xyz", "challenge-abc")):
            code_verifier, final_url = pmod._platform_authorize(sess, "user@example.com", screen_hint="login")

        self.assertEqual(code_verifier, "verifier-xyz")
        url = captured["url"]
        self.assertTrue(url.startswith("https://auth.openai.com/api/accounts/authorize?"))
        from urllib.parse import urlparse, parse_qs
        params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        self.assertEqual(params["client_id"], "app_2SKx67EdpoN0G6j64rFvigXD")
        self.assertEqual(params["redirect_uri"], "https://platform.openai.com/auth/callback")
        self.assertEqual(params["audience"], "https://api.openai.com/v1")
        self.assertEqual(params["issuer"], "https://auth.openai.com")
        self.assertEqual(params["device_id"], sess.device_id)
        self.assertEqual(params["screen_hint"], "login")
        self.assertEqual(params["max_age"], "0")
        self.assertEqual(params["login_hint"], "user@example.com")
        self.assertEqual(params["scope"], "openid profile email offline_access")
        self.assertEqual(params["response_type"], "code")
        self.assertEqual(params["response_mode"], "query")
        self.assertEqual(params["code_challenge"], "challenge-abc")
        self.assertEqual(params["code_challenge_method"], "S256")
        self.assertEqual(params["auth0Client"], pmod._platform_cfg("PLATFORM_OAUTH_AUTH0_CLIENT"))
        self.assertIn("state", params)
        self.assertIn("nonce", params)


class AddPhoneDegradationTests(unittest.TestCase):
    def test_run_fails_with_switch_hint_when_add_phone_required(self):
        sess = _fake_session()

        fake_token = {
            "access_token": _make_jwt({
                "exp": 1900000000, "iat": 1700000000,
                "https://api.openai.com/auth": {"chatgpt_account_id": "acc-123"},
                "https://api.openai.com/profile": {"email": "user@example.com"},
            }),
            "refresh_token": "rt",
            "id_token": "",
        }

        def fake_authorize(session, email, screen_hint="login"):
            # authorize 后无直出 code → 进入 continue/OTP 分支
            return "verifier", "https://auth.openai.com/log-in"

        def fake_extract(session, continue_url):
            raise RuntimeError("不应走到 callback 提取")

        with patch.object(pmod, "network_preflight"), \
             patch.object(pmod, "human_delay"), \
             patch.object(pmod.BrowserSession, "__new__", lambda *a, **k: sess), \
             patch.object(pmod, "_platform_authorize", side_effect=fake_authorize), \
             patch.object(pmod, "_authorize_continue_login", return_value={}), \
             patch.object(pmod, "_send_passwordless_otp", return_value=None), \
             patch.object(pmod, "_validate_platform_otp", return_value={
                 "continue_url": "https://auth.openai.com/add-phone",
             }), \
             patch.object(pmod, "_extract_platform_callback", side_effect=fake_extract), \
             patch.object(pmod, "_exchange_platform_token", return_value=fake_token):
            # otp_provider 直接返回一个假验证码
            result = pmod.run_platform_codex_oauth(
                "user@example.com",
                otp_provider=lambda email, after_ts=None: "123456",
                force=True,
            )

        self.assertEqual(result.get("status"), "failed")
        self.assertIn("切回", result.get("message", ""))
        self.assertIn("手机验证", result.get("message", ""))


class BuildStorageTests(unittest.TestCase):
    def test_storage_keys_and_timestamps_from_jwt(self):
        token_resp = {
            "access_token": _make_jwt({
                "exp": 1900000000, "iat": 1700000000,
                "https://api.openai.com/auth": {"chatgpt_account_id": "acc-123"},
                "https://api.openai.com/profile": {"email": "jwt@example.com"},
            }),
            "refresh_token": "rt",
            "id_token": _make_jwt({"email": "idt@example.com"}),
        }
        storage = pmod._build_platform_storage(token_resp, "fallback@example.com")
        # 键集与 grok2api build_export_item 完全一致
        self.assertEqual(
            set(storage.keys()),
            {"type", "email", "account_id", "access_token", "refresh_token", "id_token", "expired", "last_refresh"},
        )
        self.assertEqual(storage["type"], "codex")
        self.assertEqual(storage["email"], "jwt@example.com")
        self.assertEqual(storage["account_id"], "acc-123")
        # 时间戳取 JWT exp/iat，格式 UTC Z
        self.assertTrue(storage["expired"].endswith("Z"))
        self.assertTrue(storage["last_refresh"].endswith("Z"))
        from datetime import datetime, timezone
        expected_expired = datetime.fromtimestamp(1900000000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(storage["expired"], expected_expired)
        expected_last = datetime.fromtimestamp(1700000000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(storage["last_refresh"], expected_last)

    def test_credential_file_name_no_plan_suffix(self):
        self.assertEqual(pmod._platform_credential_file_name("user@example.com"), "codex-user@example.com.json")
        # 非法字符清洗
        self.assertEqual(pmod._platform_credential_file_name("a b+c@example.com"), "codex-a-b-c@example.com.json")


class UploadCpaAuthFileTests(unittest.TestCase):
    def test_upload_url_headers_multipart_and_retry(self):
        calls = []

        class FakeCurlSession:
            def __init__(self):
                self.proxies = {}

            def post(self, url, headers=None, files=None, timeout=None):
                calls.append({"url": url, "headers": headers, "files": files, "timeout": timeout})
                if len(calls) == 1:
                    return FakeResp(status_code=500, text="server boom")
                return FakeResp(status_code=200, json_data={"ok": True})

        with patch.object(pmod.proto, "_cpa_management_origin", return_value="http://127.0.0.1:8317"), \
             patch.object(pmod.proto, "_cpa_management_key", return_value="key-123"), \
             patch.object(pmod.curl_requests, "Session", side_effect=lambda *a, **k: FakeCurlSession()), \
             patch.object(pmod, "time") as fake_time:
            fake_time.sleep = MagicMock()
            payload = {"type": "codex", "email": "user@example.com"}
            result = pmod._upload_cpa_auth_file("codex-user@example.com.json", payload)

        # 第一次 500 后重试成功（CPA_CALLBACK_SUBMIT_RETRIES>=2）
        self.assertEqual(len(calls), 2)
        first = calls[0]
        self.assertEqual(first["url"], "http://127.0.0.1:8317/v0/management/auth-files")
        self.assertEqual(first["headers"]["Authorization"], "Bearer key-123")
        self.assertEqual(first["headers"]["X-Management-Key"], "key-123")
        fname, body, ctype = first["files"]["file"]
        self.assertEqual(fname, "codex-user@example.com.json")
        self.assertEqual(ctype, "application/json")
        self.assertEqual(json.loads(body.decode("utf-8")), payload)
        self.assertTrue(result.get("ok"))


class DriverDispatchTests(unittest.TestCase):
    def test_run_codex_oauth_dispatches_platform(self):
        from core import codex_oauth as proto
        fake_result = {"status": "success", "ok": True}
        with patch("config.codex.ENABLE_CODEX_AUTO", True), \
             patch("config.codex.CODEX_OAUTH_DRIVER", "platform"), \
             patch.object(pmod, "run_platform_codex_oauth", return_value=fake_result) as fake_run:
            result = proto.run_codex_oauth("user@example.com", force=True)
        self.assertEqual(result, fake_result)
        fake_run.assert_called_once()
        self.assertEqual(fake_run.call_args[0][0], "user@example.com")


if __name__ == "__main__":
    unittest.main()
