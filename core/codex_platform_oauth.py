# -*- coding: utf-8 -*-
"""
Platform OAuth 免接码 Codex 授权模块（移植自 D:\\xjx\\gptGrok2api services/register/openai_register.py）。

核心原理：不使用 CLIProxyAPI 的 Codex CLI client（app_EMoamEEZ73f0CkXaXp7hrann，会触发 add-phone），
改用 platform.openai.com 官网 client（app_2SKx67EdpoN0G6j64rFvigXD）。该 client 的
authorize + 邮箱 OTP 登录流全程不触发手机验证（grok2api 生产验证，其代码零 add-phone 处理），
platform token 上传 CPA 作 type=codex auth-file 可被 CPA 的 codex 通道正常使用。

完整接口链（均 auth.openai.com，platform client 参数）：
    1. GET  /api/accounts/authorize           issuer/client_id/audience/redirect_uri=device_id/
                                             screen_hint/login_hint/scope/PKCE(S256)/auth0Client
    2. POST /api/accounts/authorize/continue  {"username":{"kind":"email","value":邮箱}}  带 sentinel(authorize_continue)
    3. POST /api/accounts/passwordless/send-otp  {}                                登录流专用发码端点
    4. POST /api/accounts/email-otp/validate  {"code":"xxx"}                        带 sentinel(authorize_continue)
    5. 提取 callback code：continue_url 直出 → consent 逐跳跟随 → workspace/select → organization/select
       callback 指向 https://platform.openai.com/auth/callback?code=...
    6. POST /api/accounts/oauth/token（新版 JSON，带 auth0-client 头）
       或   /oauth/token（legacy form-encoded，脱离 cookie 的裸 session）换 access/refresh token
    7. 解 JWT 组装 codex-{email}.json → SQLite 落库 → multipart 上传 CPA auth-files

与 CODEX_AUTH_URL_SOURCE（cpa/sub2/local）完全正交：platform 驱动本地生成 PKCE、本地换 token，
不调用 CPA 的 codex-auth-url / oauth-callback 接口。
"""
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse, parse_qs

from config import codex as _cfg
from core.session import BrowserSession
from core.humanize import delay as human_delay
from core.openai_auth import (
    AccountUnusableError,
    _extract_error_code,
    detect_account_unusable_response_body,
    request_sentinel_token,
    build_sentinel_header,
    network_preflight,
)
from core import db
from curl_cffi import requests as curl_requests

from core import codex_oauth as proto

logger = logging.getLogger(__name__)

_AUTH_BASE = "https://auth.openai.com"
_PLATFORM_BASE = "https://platform.openai.com"

# consent 逐跳跟随的最大跳数（grok2api extract_callback_via_consent 用 10）
_MAX_CONSENT_HOPS = 10

# 平台要求手机验证时提示切换驱动的文案
_PHONE_REQUIRED_HINT = (
    "OpenAI 在 platform 流程中要求手机验证，免接码流程不适用；"
    "请把 CODEX_OAUTH_DRIVER 切回 protocol / cloak 等接码驱动后补跑"
)

# 命中这些落点说明 OpenAI 要求重新登录（登录态不足/不存在），免验证提取无意义，直接转邮箱 OTP 登录兜底
_LOGIN_REQUIRED_PATHS = (
    "/email-verification",
    "/log-in",
    "/login",
    "/create-account",
    "/create-account/password",
    "/signup",
    "/sign-in",
)


class _SigninSessionInvalidError(RuntimeError):
    """
    passwordless 登录会话失效（409 invalid_state：sign-in session is no longer valid）。

    对齐 grok2api _passwordless_login 的判定：捕获后重置 auth.openai.com cookie 并
    重新发起一次 platform authorize，再走一轮提交邮箱/发码/校验，避免把一次性会话
    失效误判为账号/流程错误。
    """


def _is_signin_session_invalid_body(body: str) -> bool:
    """判定 409 响应体是否属于"sign-in session is no longer valid"式会话失效。"""
    text = str(body or "").lower()
    return "invalid_state" in text or "sign-in session is no longer valid" in text


def _platform_cfg(key: str, default: str = "") -> str:
    """读 platform 协议常量；支持 WebUI 热加载（config.reload_all()）。"""
    return str(getattr(_cfg, key, default) or default).strip()


def _platform_callback_url() -> str:
    return _platform_cfg("PLATFORM_OAUTH_REDIRECT_URI", "https://platform.openai.com/auth/callback")


def _is_platform_callback(url: str) -> bool:
    """判断 URL 是否命中 platform 的 redirect_uri（platform.openai.com/auth/callback）。"""
    try:
        parsed = urlparse(url or "")
    except Exception:
        return False
    return (parsed.scheme in ("http", "https")
            and (parsed.hostname or "").lower() == "platform.openai.com"
            and parsed.path == "/auth/callback")


def _url_path(url: str) -> str:
    try:
        return (urlparse(url or "").path or "").lower()
    except Exception:
        return ""


def _absolute_auth_url(url: str) -> str:
    """相对 URL 补 auth.openai.com 前缀。"""
    url = str(url or "").strip()
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return _AUTH_BASE + "/" + url.lstrip("/")


# ============================================================
# 步骤 1：platform authorize（grok2api _platform_authorize 移植）
# ============================================================

def _platform_authorize(session: BrowserSession, email: str, screen_hint: str = "login_or_signup") -> tuple[str, str]:
    """
    GET platform authorize URL 并跟随重定向，返回 (final_url, 落点页面类型)。

    参数集对照 grok2api PlatformRegistrar._platform_authorize：
    issuer/client_id/audience/redirect_uri/device_id/screen_hint/max_age/login_hint/
    scope/response_type/response_mode/state/nonce/code_challenge(S256)/auth0Client。
    """
    # grok2api 在 GET 前就置 oai-did cookie（两个域），让 device_id 参数 / cookie / 后续
    # POST 的 oai-device-id 头保持同一设备；缺这一步会导致 sentinel 会话不连续（409）。
    for domain in (".auth.openai.com", "auth.openai.com"):
        try:
            session.session.cookies.set("oai-did", session.device_id, domain=domain, path="/")
        except Exception:
            continue
    code_verifier, code_challenge = proto._generate_pkce()
    params = {
        "issuer": _AUTH_BASE,
        "client_id": _platform_cfg("PLATFORM_OAUTH_CLIENT_ID", "app_2SKx67EdpoN0G6j64rFvigXD"),
        "audience": _platform_cfg("PLATFORM_OAUTH_AUDIENCE", "https://api.openai.com/v1"),
        "redirect_uri": _platform_callback_url(),
        "device_id": session.device_id,
        "screen_hint": screen_hint,
        "max_age": "0",
        "login_hint": email,
        "scope": _platform_cfg("PLATFORM_OAUTH_SCOPE", "openid profile email offline_access"),
        "response_type": "code",
        "response_mode": "query",
        "state": secrets.token_urlsafe(32),
        "nonce": secrets.token_urlsafe(32),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "auth0Client": _platform_cfg("PLATFORM_OAUTH_AUTH0_CLIENT"),
    }
    target_url = f"{_AUTH_BASE}/api/accounts/authorize?{urlencode(params)}"
    headers = session.get_auth_navigate_headers(
        referer=f"{_PLATFORM_BASE}/",
        target_origin=_AUTH_BASE,
    )
    logger.info("[Codex][Platform] 跟随 platform authorize URL 建立会话...")
    resp = proto._with_net_retry(
        "platform authorize",
        lambda: session.get(target_url, headers=headers, allow_redirects=True),
    )
    final_url = str(getattr(resp, "url", "") or "")
    status = int(getattr(resp, "status_code", 0) or 0)
    if status != 200:
        raise RuntimeError(
            f"[Codex][Platform] platform authorize 失败 status={status}: {(resp.text or '')[:300]}"
        )
    landed = ""
    try:
        data = resp.json()
        page = data.get("page") if isinstance(data, dict) else None
        if isinstance(page, dict):
            landed = str(page.get("type") or "")
    except Exception:
        landed = "login" if "/log-in" in final_url.lower() else ""
    logger.info(f"[Codex][Platform] authorize 落点: {final_url[:160]}, landed={landed or '?'}")
    return code_verifier, final_url


def _reset_auth_cookies(session: BrowserSession) -> None:
    """删除 auth.openai.com 域全部 cookie 后重设 oai-did（grok2api _reset_auth_cookies 移植）。"""
    try:
        jar = getattr(session.session.cookies, "jar", session.session.cookies)
        for cookie in list(jar):
            domain = str(getattr(cookie, "domain", "") or "")
            if "auth.openai.com" not in domain:
                continue
            try:
                session.session.cookies.delete(
                    str(getattr(cookie, "name", "") or ""),
                    domain=domain,
                    path=str(getattr(cookie, "path", "/") or "/"),
                )
            except Exception:
                continue
    except Exception:
        pass
    for domain in (".auth.openai.com", "auth.openai.com"):
        try:
            session.session.cookies.set("oai-did", session.device_id, domain=domain, path="/")
        except Exception:
            continue


# ============================================================
# 步骤 2：提交邮箱（grok2api _authorize_continue_login 移植）
# ============================================================

def _authorize_continue_login(session: BrowserSession, email: str) -> dict:
    """
    POST authorize/continue 提交邮箱进入登录验证，返回响应 JSON。

    409 invalid_state（登录会话失效）时抛 _SigninSessionInvalidError，由调用方
    _passwordless_login_branch 统一做「重置 cookie + 重新 authorize(login_or_signup)
    + 重走一轮」并同步取回最新 code_verifier（此处不内联重试，避免丢 verifier）。
    passwordless 被禁时抛错并提示切回接码驱动。
    """
    sentinel_resp = request_sentinel_token(session, "authorize_continue")
    sentinel_header, so_header = build_sentinel_header(session, sentinel_resp, "authorize_continue")
    payload = {"username": {"kind": "email", "value": email}}

    resp = _post_platform_json(
        session,
        f"{_AUTH_BASE}/api/accounts/authorize/continue",
        payload,
        referer=f"{_AUTH_BASE}/log-in?usernameKind=email",
        sentinel_header=sentinel_header,
        so_header=so_header,
    )
    status = int(getattr(resp, "status_code", 0) or 0)
    if status != 200:
        if status == 409 and _is_signin_session_invalid_body(resp.text or ""):
            raise _SigninSessionInvalidError(
                f"[Codex][Platform] 提交邮箱时登录会话失效 status={status}: {(resp.text or '')[:300]}"
            )
        raise RuntimeError(
            f"[Codex][Platform] 提交邮箱失败 status={status}: {(resp.text or '')[:300]}"
        )
    data = proto._resp_json(resp)
    page_payload = ((data.get("page") or {}).get("payload") or {}) if isinstance(data, dict) else {}
    if isinstance(page_payload, dict) and page_payload.get("passwordless_disabled"):
        raise RuntimeError(
            "OpenAI 返回的登录流不支持 passwordless（passwordless_disabled），"
            "请把 CODEX_OAUTH_DRIVER 切回 protocol / cloak 等接码驱动"
        )
    logger.info(f"[Codex][Platform] 已提交邮箱 {email}，进入登录验证")
    return data


def _post_platform_json(session: BrowserSession, url: str, payload: dict, referer: str,
                        sentinel_header: str | None = None, so_header: str | None = None):
    """
    发 platform /api/accounts/* 的 JSON POST。

    与 grok2api 的 _json_headers 对齐：除 auth JSON 头外，额外补齐 oai-* 设备上下文头
    （重点是 oai-device-id，须与 authorize GET 置的 oai-did cookie / device_id 参数一致），
    否则 sentinel 登录会话会因设备不连续返回 409 "sign-in session is no longer valid"。
    """
    headers = session.get_auth_headers(referer=referer)
    if sentinel_header:
        headers["openai-sentinel-token"] = sentinel_header
    if so_header:
        headers["openai-sentinel-so-token"] = so_header
    # grok2api _json_headers 只带 oai-device-id；这里补齐 oai 设备上下文头以维持设备连续性。
    try:
        session._attach_oai_context_headers(headers)
    except Exception:
        headers["oai-device-id"] = getattr(session, "device_id", "")
    return session.post(url, headers=headers, data=json.dumps(payload), allow_redirects=False)


# ============================================================
# 步骤 3：passwordless 发码（grok2api _send_passwordless_otp 移植）
# ============================================================

def _send_passwordless_otp(session: BrowserSession) -> None:
    """POST passwordless/send-otp 触发登录验证码发送（登录流专用端点）。"""
    resp = _post_platform_json(
        session,
        f"{_AUTH_BASE}/api/accounts/passwordless/send-otp",
        {},
        referer=f"{_AUTH_BASE}/log-in/password",
    )
    status = int(getattr(resp, "status_code", 0) or 0)
    if status not in (200, 201, 204):
        if status == 409 and _is_signin_session_invalid_body(resp.text or ""):
            raise _SigninSessionInvalidError(
                f"[Codex][Platform] passwordless 发码时登录会话失效 status={status}: {(resp.text or '')[:300]}"
            )
        raise RuntimeError(
            f"[Codex][Platform] passwordless 发码失败 status={status}: {(resp.text or '')[:300]}"
        )
    logger.info("[Codex][Platform] 登录验证码已发送")


# ============================================================
# 步骤 4：校验邮箱 OTP（grok2api validate_otp + turb _submit_email_otp 错误处理）
# ============================================================

def _validate_platform_otp(session: BrowserSession, code: str) -> dict:
    """
    POST email-otp/validate 提交登录验证码，返回响应 JSON（含 continue_url）。

    账号已废检测照抄 turb _submit_email_otp；带 sentinel(authorize_continue) 头
    （turb codex 流程同端点已验证可行；若被拒可去掉重试，challenge 单次有效）。
    """
    sentinel_resp = request_sentinel_token(session, "authorize_continue")
    sentinel_header, so_header = build_sentinel_header(session, sentinel_resp, "authorize_continue")
    resp = _post_platform_json(
        session,
        f"{_AUTH_BASE}/api/accounts/email-otp/validate",
        {"code": code},
        referer=f"{_AUTH_BASE}/email-verification",
        sentinel_header=sentinel_header,
        so_header=so_header,
    )
    status = int(getattr(resp, "status_code", 0) or 0)
    if status != 200:
        if status == 409 and _is_signin_session_invalid_body(resp.text or ""):
            raise _SigninSessionInvalidError(
                f"[Codex][Platform] 校验邮箱 OTP 时登录会话失效 status={status}: {(resp.text or '')[:300]}"
            )
        error_code = _extract_error_code(resp)
        if error_code in ("account_deactivated", "account_deleted", "account_banned"):
            raise AccountUnusableError(
                f"[Codex][Platform] 账号已废（{error_code}）status={status}: {(resp.text or '')[:200]}",
                error_code=error_code,
            )
        body_error_code = detect_account_unusable_response_body(resp.text or "")
        if body_error_code:
            raise AccountUnusableError(
                f"[Codex][Platform] 账号已废（{body_error_code}）status={status}: {(resp.text or '')[:200]}",
                error_code=body_error_code,
            )
        raise RuntimeError(
            f"[Codex][Platform] 邮箱 OTP 验证失败 status={status}: {(resp.text or '')[:300]}"
        )
    logger.info("[Codex][Platform] 邮箱 OTP 验证通过")
    return proto._resp_json(resp)


# ============================================================
# 步骤 4.5：passwordless 登录兜底（grok2api _passwordless_login 移植）
# ============================================================

def _passwordless_login_branch(
    session: BrowserSession,
    email: str,
    otp_provider,
    code_verifier: str,
) -> tuple[dict, str]:
    """
    已注册账号的 passwordless 登录兜底：authorize/continue → send-otp → 等邮箱 OTP
    （最多 3 次机会，重发=passwordless/send-otp）→ email-otp/validate。

    遇 409 invalid_state（"sign-in session is no longer valid"，常见于全新 session 重登录
    或复用登录态跨 client 授权被拒）时，按 grok2api _passwordless_login 惯例重置
    auth.openai.com cookie 并重新 platform authorize（login_or_signup）后重来一轮。

    Returns:
        (email-otp/validate 响应 JSON, 最终生效的 code_verifier)。token 交换必须使用
        最后一次 authorize 生成的 code_verifier，故由本函数一并返回。
    """
    max_email_otp_attempts = 3
    for round_index in range(2):
        verifier = code_verifier
        if round_index:
            logger.warning(
                "[Codex][Platform] passwordless 登录会话失效（409），重置 cookie 后重新发起登录授权"
            )
            _reset_auth_cookies(session)
            verifier, _final_url = _platform_authorize(session, email, screen_hint="login_or_signup")
            human_delay("navigate")
        try:
            _authorize_continue_login(session, email)
            human_delay("form")

            _send_passwordless_otp(session)
            code = None
            otp_after_ts = time.time()
            for email_otp_attempt in range(1, max_email_otp_attempts + 1):
                logger.info(f"[Codex][Platform] 等待邮箱 OTP：{email}（第 {email_otp_attempt}/{max_email_otp_attempts} 次）")
                try:
                    code = otp_provider(email, after_ts=otp_after_ts)
                    break
                except Exception as exc:
                    if email_otp_attempt >= max_email_otp_attempts:
                        raise
                    logger.warning(
                        "[Codex][Platform] 一直未收到邮箱 OTP，重发后继续等待（下一轮 %s/%s）：%s: %s",
                        email_otp_attempt + 1, max_email_otp_attempts,
                        type(exc).__name__, str(exc)[:180],
                    )
                    otp_after_ts = time.time()
                    _send_passwordless_otp(session)
                    human_delay("api")
            logger.info(f"[Codex][Platform] 邮箱 OTP 收到：{code}")
            human_delay("otp_input")

            data = _validate_platform_otp(session, code)
            return data, verifier
        except _SigninSessionInvalidError:
            if round_index == 0:
                continue
            raise
    raise RuntimeError(
        "[Codex][Platform] passwordless 登录会话持续失效（409），"
        "请把 CODEX_OAUTH_DRIVER 切回 protocol / cloak 等接码驱动后补跑"
    )


# ============================================================
# callback 提取（grok2api extract_oauth_callback_params_from_url / extract_callback_via_consent 移植）
# ============================================================

def _extract_callback_params(url: str) -> dict | None:
    """解析 URL query 中的 code/state/scope；无 code 返回 None。"""
    if not url:
        return None
    try:
        params = parse_qs(urlparse(url).query)
    except Exception:
        return None
    code = str((params.get("code") or [""])[0]).strip()
    if not code:
        return None
    return {
        "code": code,
        "state": str((params.get("state") or [""])[0]).strip(),
        "scope": str((params.get("scope") or [""])[0]).strip(),
    }


def _extract_platform_callback(session: BrowserSession, continue_url: str) -> dict:
    """
    从 continue_url 起提取 platform callback code，四级尝试（照抄 grok2api
    exchange_tokens_from_continue_url + extract_callback_via_consent 的提取链）：
      1) continue_url 直接含 code
      2) 从 consent URL 起逐跳 GET（allow_redirects=False，最多 10 跳），看 Location/落点 URL
      3) workspace/select（workspace_id 从 oai-client-auth-session cookie 解）
      4) organization/select（org/project 从 workspace/select 响应的 data.orgs 取）
    """
    callback = _extract_callback_params(continue_url)
    if callback:
        return callback

    # 2) consent 逐跳跟随
    current = _absolute_auth_url(continue_url)
    if current:
        for hop in range(_MAX_CONSENT_HOPS):
            headers = session.get_auth_navigate_headers(
                referer=f"{_AUTH_BASE}/",
                target_origin=_AUTH_BASE,
            )
            resp = session.get(current, headers=headers, allow_redirects=False)
            callback = (
                _extract_callback_params(str(getattr(resp, "url", "") or ""))
                or _extract_callback_params(str(getattr(resp, "headers", {}).get("Location") or ""))
            )
            if callback:
                return callback
            location = str(getattr(resp, "headers", {}).get("Location") or "").strip()
            status = int(getattr(resp, "status_code", 0) or 0)
            if status not in (301, 302, 303, 307, 308) or not location:
                break
            current = _absolute_auth_url(location)

    # 3) workspace/select
    try:
        wid = proto._get_workspace_id(session)
    except Exception as exc:
        logger.warning("[Codex][Platform] 取 workspace_id 失败，跳过 workspace/select：%s: %s", type(exc).__name__, str(exc)[:180])
        wid = ""
    ws_data = {}
    if wid:
        ws_resp = _post_platform_json(
            session,
            f"{_AUTH_BASE}/api/accounts/workspace/select",
            {"workspace_id": wid},
            referer=f"{_AUTH_BASE}/sign-in-with-chatgpt/platform/consent",
        )
        callback = _extract_callback_params(str(getattr(ws_resp, "headers", {}).get("Location") or ""))
        if callback:
            return callback
        ws_data = proto._resp_json(ws_resp)
        callback = _extract_callback_params(str(ws_data.get("continue_url") or ""))
        if callback:
            return callback

    # 4) organization/select
    orgs = ((ws_data.get("data") or {}).get("orgs") or []) if isinstance(ws_data, dict) else []
    if not orgs:
        raise RuntimeError(
            f"[Codex][Platform] 未能从 continue_url 提取 callback code: continue={str(continue_url)[:200]}"
        )
    first_org = orgs[0] if isinstance(orgs[0], dict) else {}
    org_id = str(first_org.get("id") or "").strip()
    if not org_id:
        raise RuntimeError(f"[Codex][Platform] organization/select 缺少 org_id: {str(first_org)[:200]}")
    projects = first_org.get("projects") or []
    project_id = str(((projects[0] or {}) if projects and isinstance(projects[0], dict) else {}).get("id") or "").strip()
    body = {"org_id": org_id}
    if project_id:
        body["project_id"] = project_id
    org_resp = _post_platform_json(
        session,
        f"{_AUTH_BASE}/api/accounts/organization/select",
        body,
        referer=str(ws_data.get("continue_url") or f"{_AUTH_BASE}/sign-in-with-chatgpt/platform/consent"),
    )
    callback = _extract_callback_params(str(getattr(org_resp, "headers", {}).get("Location") or ""))
    if not callback:
        org_data = proto._resp_json(org_resp)
        callback = _extract_callback_params(str(org_data.get("continue_url") or ""))
    if not callback:
        raise RuntimeError(
            f"[Codex][Platform] organization/select 后仍未拿到 callback code: status={getattr(org_resp, 'status_code', '')}"
        )
    return callback


# ============================================================
# 换 token（grok2api request_platform_oauth_token / _legacy 移植）
# ============================================================

def _exchange_platform_token(session: BrowserSession, code: str, code_verifier: str) -> dict:
    """
    用 authorization code 换 platform token。新版 /api/accounts/oauth/token（JSON）优先，
    legacy /oauth/token（form-encoded，裸 session 不带 auth cookie）兜底；两路都失败合并错误抛出。
    """
    errors: list[str] = []

    def _fp_header(name: str) -> str:
        profile = getattr(session, "browser_profile", None) or {}
        return str(profile.get(name) or "")

    # 1) 新版 JSON 端点
    try:
        headers = session.get_auth_headers(referer=f"{_PLATFORM_BASE}/")
        headers.update({
            "origin": _PLATFORM_BASE,
            "auth0-client": _platform_cfg("PLATFORM_OAUTH_AUTH0_CLIENT"),
            "cache-control": "no-cache",
            "pragma": "no-cache",
        })
        ua = _fp_header("user_agent")
        if ua:
            headers["user-agent"] = ua
        resp = session.post(
            f"{_AUTH_BASE}/api/accounts/oauth/token",
            headers=headers,
            data=json.dumps({
                "client_id": _platform_cfg("PLATFORM_OAUTH_CLIENT_ID", "app_2SKx67EdpoN0G6j64rFvigXD"),
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _platform_callback_url(),
            }),
        )
        status = int(getattr(resp, "status_code", 0) or 0)
        if status == 200:
            data = proto._resp_json(resp)
            missing = [key for key in ("access_token", "refresh_token") if not data.get(key)]
            if not missing:
                logger.info("[Codex][Platform] 新版 token 端点换取成功")
                return data
            errors.append(f"api token 返回缺少字段: {', '.join(missing)}")
        else:
            errors.append(f"api token 接口拒绝: status={status}, {(resp.text or '')[:200]}")
    except Exception as exc:
        errors.append(f"api token 请求异常: {type(exc).__name__}: {str(exc)[:200]}")

    # 2) legacy form-encoded 端点（grok2api fresh_session=True：脱离 auth cookie 的裸 session）
    try:
        from core.session import IMPERSONATE as _IMPERSONATE
        bare = curl_requests.Session(impersonate=_IMPERSONATE)
        if session.proxy:
            bare.proxies = {"http": session.proxy, "https": session.proxy}
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _platform_callback_url(),
            "client_id": _platform_cfg("PLATFORM_OAUTH_CLIENT_ID", "app_2SKx67EdpoN0G6j64rFvigXD"),
            "code_verifier": code_verifier,
        }
        resp = bare.post(
            f"{_AUTH_BASE}/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data=urlencode(form),
            timeout=int(getattr(_cfg, "CPA_REQUEST_TIMEOUT", 30) or 30),
        )
        status = int(getattr(resp, "status_code", 0) or 0)
        if status == 200:
            data = proto._resp_json(resp)
            missing = [key for key in ("access_token", "refresh_token") if not data.get(key)]
            if not missing:
                logger.info("[Codex][Platform] legacy token 端点换取成功")
                return data
            errors.append(f"legacy token 返回缺少字段: {', '.join(missing)}")
        else:
            errors.append(f"legacy token 接口拒绝: status={status}, {(resp.text or '')[:200]}")
    except Exception as exc:
        errors.append(f"legacy token 请求异常: {type(exc).__name__}: {str(exc)[:200]}")

    detail = "；".join(errors[-4:]) if errors else "未返回 token"
    raise RuntimeError(f"[Codex][Platform] 换 token 失败: {detail}")


# ============================================================
# 凭证组装（grok2api build_export_item 移植，时间戳取 JWT exp/iat）
# ============================================================

def _ts_to_iso(value) -> str:
    """Unix 秒时间戳转 UTC ISO（turb 的 Z 格式）；非法返回空串。"""
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_platform_storage(token_resp: dict, email: str) -> dict:
    """
    组装与 grok2api build_export_item 键集完全一致的 codex 凭证 JSON：
    {type, email, account_id, access_token, refresh_token, id_token, expired, last_refresh}
    account_id/email 优先从 access_token JWT 解，id_token 兜底；
    expired/last_refresh 取 access_token JWT 的 exp/iat（platform 响应 expires_in 可能缺失）。
    """
    access_token = str(token_resp.get("access_token") or "").strip()
    refresh_token = str(token_resp.get("refresh_token") or "").strip()
    id_token = str(token_resp.get("id_token") or "").strip()
    if not access_token or not refresh_token:
        raise RuntimeError("[Codex][Platform] token 响应缺少 access_token/refresh_token")

    access_payload = {}
    try:
        parts = access_token.split(".")
        if len(parts) >= 2:
            access_payload = proto._decode_jwt_segment(parts[1])
    except Exception:
        access_payload = {}
    auth_claim = access_payload.get("https://api.openai.com/auth")
    auth_claim = auth_claim if isinstance(auth_claim, dict) else {}
    profile_claim = access_payload.get("https://api.openai.com/profile")
    profile_claim = profile_claim if isinstance(profile_claim, dict) else {}

    id_claims = proto._parse_id_token(id_token)
    account_email = (
        str(profile_claim.get("email") or "").strip()
        or id_claims.get("email", "")
        or (email or "").strip()
    )
    account_id = (
        str(auth_claim.get("chatgpt_account_id") or "").strip()
        or id_claims.get("account_id", "")
    )

    expired = _ts_to_iso(access_payload.get("exp"))
    last_refresh = _ts_to_iso(access_payload.get("iat"))
    if not expired:
        # 兜底：JWT 无 exp 时用 expires_in 推
        expires_in = token_resp.get("expires_in", 0) or 0
        if expires_in:
            expired = (datetime.now(timezone.utc) + proto._timedelta_seconds(expires_in)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not last_refresh:
        last_refresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "type": "codex",
        "email": account_email,
        "account_id": account_id,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "expired": expired,
        "last_refresh": last_refresh,
    }


def _platform_credential_file_name(email: str) -> str:
    """codex-{safe_email}.json（无 plan 后缀，对齐 grok2api/CPA 侧契约）。"""
    identity = str(email or "").strip() or "oauth"
    safe = "".join(ch if (ch.isalnum() or ch in "@._-") else "-" for ch in identity).strip("-._")[:120]
    return f"codex-{safe or 'oauth'}.json"


# ============================================================
# CPA auth-files 上传（grok2api _upload_auth_file 移植 + turb 重试惯例）
# ============================================================

def _upload_cpa_auth_file(file_name: str, payload: dict) -> dict:
    """
    multipart 上传 codex-{email}.json 到 CPA POST /v0/management/auth-files。
    重试结构照抄 proto._submit_cpa_callback（CPA_CALLBACK_SUBMIT_RETRIES + _is_cpa_callback_retryable）。

    用 requests（非 curl_cffi）：curl_cffi 不支持 files= 参数
    (NotImplementedError: files is not supported, use `multipart`)，而本上传是
    管理接口调用、不需要 TLS 指纹伪装。
    """
    import requests
    origin = proto._cpa_management_origin()
    key = proto._cpa_management_key()
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {key}",
        "X-Management-Key": key,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    max_attempts = max(1, int(getattr(_cfg, "CPA_CALLBACK_SUBMIT_RETRIES", 5) or 5))
    base_delay = max(1.0, float(getattr(_cfg, "CPA_CALLBACK_SUBMIT_RETRY_DELAY", 6) or 6))
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            logger.info("[Codex][Platform] 正在上传 CPA auth-file（第 %s/%s 次）: %s", attempt, max_attempts, file_name)
            resp = requests.post(
                f"{origin}/v0/management/auth-files",
                headers=headers,
                files={"file": (file_name, body, "application/json")},
                timeout=int(getattr(_cfg, "CPA_REQUEST_TIMEOUT", 30) or 30),
            )
            if 200 <= int(resp.status_code) < 300:
                logger.info("[Codex][Platform] CPA auth-file 上传成功: %s", file_name)
                return proto._resp_json(resp)
            raise RuntimeError(
                f"[Codex][Platform] CPA auth-file 上传失败 status={resp.status_code}: {(resp.text or '')[:300]}"
            )
        except Exception as exc:
            last_exc = exc
            retryable = proto._is_cpa_callback_retryable(exc)
            if attempt >= max_attempts or not retryable:
                logger.warning(
                    "[Codex][Platform] CPA auth-file 上传失败且不再重试：attempt=%s/%s retryable=%s error=%s",
                    attempt, max_attempts, retryable, exc,
                )
                raise
            delay = base_delay * attempt
            logger.warning(
                "[Codex][Platform] CPA auth-file 上传失败，将在 %.1fs 后重试：attempt=%s/%s error=%s",
                delay, attempt, max_attempts, exc,
            )
            time.sleep(delay)
    raise RuntimeError(f"[Codex][Platform] CPA auth-file 上传失败：{last_exc}")


# ============================================================
# 主入口
# ============================================================

def run_platform_codex_oauth(
    email: str,
    otp_provider=None,
    proxy: str | None = None,
    force: bool = False,
    session=None,
) -> dict:
    """
    Platform OAuth 免接码 Codex 授权（移植自 grok2api）。

    优先复用注册流程已登录的 BrowserSession（session 参数）：authorize(screen_hint=login)
    直接放行 → consent/workspace/org 提取 callback code → 本地换 token，全程零验证码；
    未传 session（或登录态不足以直接放行）时，降级 passwordless 邮箱 OTP 登录兜底
    （仅消耗 1 封邮箱 OTP，不触手机验证码；409 会话失效自动重置重试）。

    Args:
        session: 注册流程保留下来的已登录 BrowserSession（protocol 驱动注册后传入）。
                 为 None 时内部新建全新 BrowserSession，从重登录开始走邮箱 OTP 兜底。

    Returns:
        与 proto._codex_result 同形态 dict。任何异常都被吞掉转 failed/deactivated，不向上抛。
    """
    if not force and not _cfg.ENABLE_CODEX_AUTO:
        return proto._codex_result(status="skipped", message="ENABLE_CODEX_AUTO=False")
    if not email:
        return proto._codex_result(status="skipped", message="email 为空")

    if otp_provider is None:
        from core.email_provider import wait_for_otp as otp_provider

    reuse_logged_in = session is not None
    if not reuse_logged_in:
        session = BrowserSession(proxy=proxy, fingerprint_seed=f"account:{email.lower()}")
    try:
        screen_hint = "login" if reuse_logged_in else "login_or_signup"
        if reuse_logged_in:
            logger.info(f"[Codex][Platform] 开始免接码授权（复用注册登录态）：{email}")
        else:
            logger.info(f"[Codex][Platform] 开始免接码授权（全新 session）：{email}")

        # 1. 代理预检 + platform authorize（login_hint 可能直接触发发码，先记录收信边界）
        #    复用已登录会话时跳过预检/新会话建立，保持与注册一致的 device_id / 代理 / cookie。
        if not reuse_logged_in:
            network_preflight(session)
        human_delay("navigate")
        code_verifier, final_url = _platform_authorize(session, email, screen_hint=screen_hint)
        human_delay("navigate")

        callback = _extract_callback_params(final_url)
        if not callback and reuse_logged_in:
            # 1.5 已登录会话 authorize 后通常直接放行；未直出 code 时尝试
            #     consent/workspace/org 提取链（免验证路径）。落点若要求重新登录则跳过，
            #     避免在未登录会话上打 workspace/organization/select。
            if _url_path(final_url) not in _LOGIN_REQUIRED_PATHS:
                try:
                    callback = _extract_platform_callback(session, final_url)
                    logger.info("[Codex][Platform] 从登录态落点直接提取到 callback（免验证路径）")
                except Exception as exc:
                    callback = None
                    logger.warning(
                        "[Codex][Platform] 登录态落点未直接给出 callback（%s: %s），转入邮箱 OTP 登录兜底",
                        type(exc).__name__, str(exc)[:150],
                    )
        if not callback:
            # 2. passwordless 邮箱 OTP 登录兜底（含 409 会话失效重置重试）
            data, code_verifier = _passwordless_login_branch(
                session, email, otp_provider, code_verifier=code_verifier,
            )
            human_delay("api")
            continue_url = str((data or {}).get("continue_url") or "").strip() \
                or f"{_AUTH_BASE}/sign-in-with-chatgpt/platform/consent"

            # 风控在 platform 流程中插手机验证 → 免接码流程不适用，立即失败提示切驱动
            path = _url_path(continue_url)
            if "/add-phone" in path or "/phone-verification" in path:
                raise RuntimeError(_PHONE_REQUIRED_HINT)

            # 3. 提取 callback code（continue_url → consent → workspace → organization）
            callback = _extract_platform_callback(session, continue_url)

        code = str(callback.get("code") or "").strip()
        if not code:
            raise RuntimeError("[Codex][Platform] 未能拿到 callback code")
        # state 不做强校验（对照 grok2api 宽松策略），仅记录。
        logger.info(f"[Codex][Platform] 已拿到 authorization code：{code[:16]}...")

        # 6. 本地换 token（新版优先，legacy 兜底）
        token_resp = _exchange_platform_token(session, code, code_verifier)

        # 7. 组装凭证 → SQLite 落库（不走 save_codex_credential，避免 plan 后缀）
        storage = _build_platform_storage(token_resp, email)
        effective_email = storage.get("email") or email
        fname = _platform_credential_file_name(effective_email)
        db.upsert_codex_credential(storage, fname)
        path = f"sqlite://codex_accounts/{fname}"

        # 8. 上传 CPA auth-files
        if bool(getattr(_cfg, "PLATFORM_OAUTH_UPLOAD_TO_CPA", True)):
            _upload_cpa_auth_file(fname, storage)
            msg = f"platform 免接码授权成功，已上传 CPA auth-file: {fname}"
        else:
            msg = f"platform 免接码授权成功（仅本地保存）: {fname}"

        logger.info(
            f"[Codex][Platform] 成功：{effective_email}，account_id={storage.get('account_id') or 'unknown'}，"
            f"已保存到 {path}"
        )
        return proto._codex_result(
            status="success",
            ok=True,
            email=effective_email,
            file_path=path,
            callback_url=f"{_platform_callback_url()}?code={code[:12]}...",
            message=msg,
        )
    except AccountUnusableError as exc:
        logger.warning(f"[Codex][Platform] 账号已废（{exc.error_code}）：{email}")
        return proto._codex_result(
            status="deactivated",
            email=email,
            message=f"账号已废（{exc.error_code}）",
        )
    except Exception as exc:
        logger.warning(f"[Codex][Platform] 失败：{email}，{type(exc).__name__}: {str(exc)[:200]}")
        logger.debug("[Codex][Platform] 失败详情:", exc_info=True)
        return proto._codex_result(
            status="failed",
            email=email,
            message=f"{type(exc).__name__}: {str(exc)[:200]}",
        )
