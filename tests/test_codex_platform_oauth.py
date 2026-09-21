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

        def fake_post(url, headers=None, files=None, timeout=None):
            calls.append({"url": url, "headers": headers, "files": files, "timeout": timeout})
            if len(calls) == 1:
                return FakeResp(status_code=500, text="server boom")
            return FakeResp(status_code=200, json_data={"ok": True})

        with patch.object(pmod.proto, "_cpa_management_origin", return_value="http://127.0.0.1:8317"), \
             patch.object(pmod.proto, "_cpa_management_key", return_value="key-123"), \
             patch.object(pmod.requests, "post", side_effect=fake_post), \
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

    def test_run_codex_oauth_passes_registration_session_to_platform(self):
        from core import codex_oauth as proto
        fake_result = {"status": "success", "ok": True}
        reg_session = _fake_session()
        with patch("config.codex.ENABLE_CODEX_AUTO", True), \
             patch("config.codex.CODEX_OAUTH_DRIVER", "platform"), \
             patch.object(pmod, "run_platform_codex_oauth", return_value=fake_result) as fake_run:
            result = proto.run_codex_oauth("user@example.com", force=True, session=reg_session)
        self.assertEqual(result, fake_result)
        fake_run.assert_called_once()
        self.assertIs(fake_run.call_args.kwargs["session"], reg_session)


def _fake_token():
    return {
        "access_token": _make_jwt({
            "exp": 1900000000, "iat": 1700000000,
            "https://api.openai.com/auth": {"chatgpt_account_id": "acc-123"},
            "https://api.openai.com/profile": {"email": "user@example.com"},
        }),
        "refresh_token": "rt",
        "id_token": "",
    }


class ReuseSessionTests(unittest.TestCase):
    """复用注册已登录 session 的免验证路径（grok2api extract_platform_oauth_credentials 对齐）。"""

    def _run(self, authorize_url, sess=None, otp_calls=None):
        token = _fake_token()
        with patch.object(pmod, "network_preflight") as fake_preflight, \
             patch.object(pmod, "human_delay"), \
             patch.object(pmod, "_exchange_platform_token", return_value=token) as fake_exchange, \
             patch.object(pmod, "db") as fake_db, \
             patch.object(pmod, "_upload_cpa_auth_file", return_value={"ok": True}):
            if sess is None:
                sess = _fake_session()
            result = pmod.run_platform_codex_oauth(
                "user@example.com",
                otp_provider=otp_calls or (lambda email, after_ts=None: "123456"),
                force=True,
                session=sess,
            )
        return result, sess, fake_preflight, fake_exchange

    def test_reuse_session_direct_code_zero_otp(self):
        sess = _fake_session()
        with patch.object(pmod, "_platform_authorize", return_value=(
                "verifier-x",
                "https://platform.openai.com/auth/callback?code=abc123&state=s",
        )) as fake_auth:
            result, used_sess, fake_preflight, _ = self._run("", sess=sess)
        self.assertEqual(result.get("status"), "success")
        # 复用登录态：screen_hint=login，不新建 session、不做网络预检
        fake_auth.assert_called_once()
        self.assertEqual(fake_auth.call_args.kwargs["screen_hint"], "login")
        self.assertIs(used_sess, sess)
        fake_preflight.assert_not_called()

    def test_reuse_session_never_creates_new_browser_session(self):
        sess = _fake_session()
        def boom(*a, **k):
            raise AssertionError("不应新建 BrowserSession")
        with patch.object(pmod, "_platform_authorize", return_value=(
                "verifier-x", "https://platform.openai.com/auth/callback?code=abc")), \
             patch.object(pmod.BrowserSession, "__new__", side_effect=boom), \
             patch.object(pmod, "human_delay"), \
             patch.object(pmod, "_exchange_platform_token", return_value=_fake_token()), \
             patch.object(pmod, "db"), \
             patch.object(pmod, "_upload_cpa_auth_file", return_value={"ok": True}):
            result = pmod.run_platform_codex_oauth(
                "user@example.com",
                otp_provider=lambda email, after_ts=None: "123456",
                force=True,
                session=sess,
            )
        self.assertEqual(result.get("status"), "success")

    def test_reuse_session_landing_email_verification_skips_noauth_extract_and_uses_otp_fallback(self):
        """登录态不足以放行（落点 email-verification）→ 不得打免验证提取链，直接走邮箱 OTP 兜底。"""
        sess = _fake_session()
        with patch.object(pmod, "_platform_authorize", return_value=(
                "verifier-x", "https://auth.openai.com/email-verification")), \
             patch.object(pmod, "_extract_platform_callback", return_value={"code": "from-continue"}) as fake_extract, \
             patch.object(pmod, "_passwordless_login_branch", return_value=(
                 {"continue_url": "https://auth.openai.com/sign-in-with-chatgpt/platform/consent"},
                 "verifier-after-login",
             )) as fake_branch:
            result, _, _, _ = self._run("", sess=sess)
        self.assertEqual(result.get("status"), "success")
        # 免验证提取只在"放行后无 code"时尝试一次；email-verification 落点直接跳登录兜底
        fake_extract.assert_called_once()
        self.assertEqual(fake_extract.call_args.args[1], "https://auth.openai.com/sign-in-with-chatgpt/platform/consent")
        fake_branch.assert_called_once()

    def test_fresh_session_still_uses_login_or_signup_hint(self):
        """未传 session（如浏览器驱动注册后的重登/补跑）保持 login_or_signup 且会新建会话。"""
        sess = _fake_session()
        with patch.object(pmod, "_platform_authorize", return_value=(
                "verifier-x", "https://auth.openai.com/email-verification")), \
             patch.object(pmod, "_extract_platform_callback", return_value={"code": "abc"}), \
             patch.object(pmod, "_passwordless_login_branch", return_value=(
                 {"continue_url": "https://auth.openai.com/consent"}, "verifier-y",
             )) as fake_branch, \
             patch.object(pmod.BrowserSession, "__new__", lambda *a, **k: sess), \
             patch.object(pmod, "network_preflight"), \
             patch.object(pmod, "human_delay"), \
             patch.object(pmod, "_exchange_platform_token", return_value=_fake_token()), \
             patch.object(pmod, "db"), \
             patch.object(pmod, "_upload_cpa_auth_file", return_value={"ok": True}):
            result = pmod.run_platform_codex_oauth(
                "user@example.com",
                otp_provider=lambda email, after_ts=None: "123456",
                force=True,
            )
        self.assertEqual(result.get("status"), "success")
        self.assertEqual(fake_branch.call_args.args[1], "user@example.com")

        # 全新 session 分支：预检执行过，且 authorize 用 login_or_signup
        with patch.object(pmod, "network_preflight") as fp, \
             patch.object(pmod, "_platform_authorize", return_value=(
                 "v", "https://platform.openai.com/auth/callback?code=abc")) as fake_auth, \
             patch.object(pmod.BrowserSession, "__new__", lambda *a, **k: sess), \
             patch.object(pmod, "human_delay"), \
             patch.object(pmod, "_exchange_platform_token", return_value=_fake_token()), \
             patch.object(pmod, "db"), \
             patch.object(pmod, "_upload_cpa_auth_file", return_value={"ok": True}):
            result2 = pmod.run_platform_codex_oauth(
                "user@example.com", otp_provider=lambda e, after_ts=None: "1", force=True)
            fp.assert_called_once()
            self.assertEqual(fake_auth.call_args.kwargs["screen_hint"], "login_or_signup")
            self.assertEqual(result2.get("status"), "success")


class SigninSessionInvalidTests(unittest.TestCase):
    def test_409_invalid_state_send_otp_raises_special_error(self):
        sess = _fake_session()
        fake_post = MagicMock()
        fake_post.status_code = 409
        fake_post.text = '{"error": {"message": "Your sign-in session is no longer valid. Please start over to continue."}}'
        with patch.object(pmod, "_post_platform_json", return_value=fake_post), \
             patch.object(pmod, "request_sentinel_token", return_value={}), \
             patch.object(pmod, "build_sentinel_header", return_value=("h", None)):
            with self.assertRaises(pmod._SigninSessionInvalidError):
                pmod._send_passwordless_otp(sess)

    def test_login_branch_resets_and_retries_once_on_invalid_state(self):
        sess = _fake_session()
        from core import codex_oauth as proto
        auth_calls = []

        def fake_authorize(session, email, screen_hint="login_or_signup"):
            auth_calls.append(screen_hint)
            # 仅在 409 重试轮（round 1）会重新 authorize；round 0 沿用主流程传入的 code_verifier
            return "verifier-after-reset", "https://auth.openai.com/email-verification"

        continue_attempts = {"n": 0}

        def fake_continue(session, email):
            continue_attempts["n"] += 1
            if continue_attempts["n"] == 1:
                raise pmod._SigninSessionInvalidError("sign-in session is no longer valid")
            return {}

        with patch.object(pmod, "_platform_authorize", side_effect=fake_authorize), \
             patch.object(pmod, "_authorize_continue_login", side_effect=fake_continue), \
             patch.object(pmod, "_send_passwordless_otp"), \
             patch.object(pmod, "_validate_platform_otp", return_value={
                 "continue_url": "https://auth.openai.com/sign-in-with-chatgpt/platform/consent",
             }), \
             patch.object(pmod, "_reset_auth_cookies") as fake_reset, \
             patch.object(pmod, "human_delay"):
            data, verifier = pmod._passwordless_login_branch(
                sess, "user@example.com",
                otp_provider=lambda email, after_ts=None: "123456",
                code_verifier="verifier-from-main",
            )
        # 409 后重置 cookie + 重新 authorize（login_or_signup），第二次成功且返回重试轮的 verifier
        self.assertEqual(continue_attempts["n"], 2)
        fake_reset.assert_called_once_with(sess)
        self.assertEqual(auth_calls, ["login_or_signup"])
        self.assertEqual(verifier, "verifier-after-reset")
        self.assertIn("continue_url", data)


if __name__ == "__main__":
    unittest.main()
