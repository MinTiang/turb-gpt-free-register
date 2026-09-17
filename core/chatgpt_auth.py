# -*- coding: utf-8 -*-
"""
ChatGPT Auth 模块
处理 chatgpt.com 域名下的认证请求（步骤1-3）
"""
import json
import logging
from urllib.parse import urlencode, urlparse, parse_qs

from core.session import BrowserSession
from config import (
    OPENAI_CLIENT_ID, OPENAI_SCOPE, OPENAI_AUDIENCE, OPENAI_REDIRECT_URI
)

logger = logging.getLogger(__name__)

# 2026-09-17 真机抓包对齐：
# - 页面链路是 首页 → /auth/login_with?...screen_hint=signup&login_hint=... 文档导航，
#   providers/csrf/signin 三个 XHR 的 referer 都是这条 login_with 完整 URL；
# - signin 查询参数顺序 prompt → screen_hint → ext-oai-did →
#   auth_session_logging_id → login_hint，screen_hint=signup；
# - NextAuth 产出的 authorize 自带 ccaps=login_methods chatgpt_login_finalizer_v1
#   和 auth_return_target_category=chatgpt_home。
_CC_CAPS = "login_methods chatgpt_login_finalizer_v1"


def _request_with_retry(session: BrowserSession, label: str, fn):
    """providers/csrf/signin 一次性 403 会误杀整个注册；复用 authorize 的
    代理链路重试（403/429/5xx/临时网络错误），保留 Cookie Jar 只清熔断。"""
    from core.openai_auth import _request_with_proxy_retry
    return _request_with_proxy_retry(session, label, fn)


def navigate_login_with(session: BrowserSession, email: str) -> str:
    """复刻浏览器点击“注册”后的 /auth/login_with 文档导航，返回最终 URL。

    这一步让后续 providers/csrf/signin 的 referer 与真实页面完全一致，
    同时把 login_hint/screen_hint 提前写进 chatgpt.com 的会话上下文。

    CF 对 document 路由的托管质询无法在协议层解决，而本步只是 referer
    卫生、非功能必需；持续被质询时降级为用首页 referer 继续主链路。
    """
    from core.openai_auth import _reset_retryable_circuit, _rotate_document_navigation_id

    query = {
        "callback_path": "/",
        "screen_hint": "signup",
        "login_hint": email,
        "auth_session_logging_id": session.auth_session_logging_id,
        "ext-oai-did": session.device_id,
    }
    url = "https://chatgpt.com/auth/login_with?" + urlencode(query)
    headers = session.get_chatgpt_navigate_headers(referer="https://chatgpt.com/")
    logger.info("[步骤2.5] 导航 /auth/login_with（对齐真实页面链路）...")
    try:
        resp = _request_with_retry(
            session,
            "login_with",
            lambda: session.get(url, headers=headers, allow_redirects=True),
        )
    except Exception as exc:
        # 质询 403（cf-mitigated: challenge）或反复失败都在这里落到降级路径。
        _reset_retryable_circuit(session)
        logger.warning(
            "[步骤2.5] login_with 导航失败（%s: %s），降级用首页 referer 继续",
            type(exc).__name__, str(exc)[:120],
        )
        return "https://chatgpt.com/"
    _rotate_document_navigation_id(session)
    final_url = str(getattr(resp, "url", "") or url)
    logger.info(f"[步骤2.5] login_with 导航完成: {final_url[:160]}")
    return final_url


def _ensure_authorize_context(
    authorize_url: str,
    session: BrowserSession,
    email: str,
    screen_hint: str = "login_or_signup",
) -> str:
    """
    对 NextAuth 返回的 authorize URL 做最后兜底：确保当前前端默认
    login_or_signup 链路的上下文参数没有在重定向生成阶段丢失。
    注册链路传 screen_hint="signup"，与 2026-09-17 真机抓包一致。
    """
    try:
        parsed = urlparse(authorize_url)
        if not parsed.netloc.endswith("auth.openai.com"):
            return authorize_url
        params = parse_qs(parsed.query, keep_blank_values=True)
        # 旧实现主动注入该字段；当前成功浏览器 authorize 已不携带。
        changed = bool(params.pop("ext-passkey-client-capabilities", None))
        required = {
            "ext-oai-did": session.device_id,
            "auth_session_logging_id": session.auth_session_logging_id,
            "screen_hint": screen_hint,
            "login_hint": email,
            "ccaps": _CC_CAPS,
            "auth_return_target_category": "chatgpt_home",
        }
        for key, value in required.items():
            if key in {"ccaps", "auth_return_target_category"}:
                if params.get(key) != [value]:
                    params[key] = [value]
                    changed = True
            elif not params.get(key):
                params[key] = [value]
                changed = True
        if not changed:
            return authorize_url
        return parsed._replace(query=urlencode(params, doseq=True)).geturl()
    except Exception:
        return authorize_url


def get_providers(session: BrowserSession, referer: str = "https://chatgpt.com/auth/login") -> dict:
    """
    步骤1: 获取 OAuth Providers 列表。
    GET https://chatgpt.com/api/auth/providers

    验证与 chatgpt.com 的连接是否正常，并获取可用的 OAuth 提供商。

    Returns:
        providers 字典，例如:
        {
            "openai": {
                "id": "openai",
                "name": "openai",
                "type": "oauth",
                "signinUrl": "https://chatgpt.com/api/auth/signin/openai",
                "callbackUrl": "https://chatgpt.com/api/auth/callback/openai"
            },
            ...
        }
    """
    url = "https://chatgpt.com/api/auth/providers"
    headers = session.get_nextauth_headers(referer=referer)

    logger.info("[步骤1] 获取 OAuth Providers...")
    resp = _request_with_retry(session, "providers", lambda: session.get(url, headers=headers))
    resp.raise_for_status()

    data = resp.json()
    logger.info(f"[步骤1] 成功获取 {len(data)} 个 providers: {list(data.keys())}")
    return data


def get_csrf_token(session: BrowserSession, referer: str = "https://chatgpt.com/auth/login") -> str:
    """
    步骤2: 获取 CSRF Token。
    GET https://chatgpt.com/api/auth/csrf

    CSRF token 将在后续 signin 请求中使用。

    Returns:
        csrfToken 字符串
    """
    url = "https://chatgpt.com/api/auth/csrf"
    headers = session.get_nextauth_headers(referer=referer)

    logger.info("[步骤2] 获取 CSRF Token...")
    resp = _request_with_retry(session, "csrf", lambda: session.get(url, headers=headers))
    resp.raise_for_status()

    data = resp.json()
    csrf_token = data.get("csrfToken", "")
    logger.info(f"[步骤2] 获取 CSRF Token 成功: {csrf_token[:20]}...")
    return csrf_token


def probe_auth_session(session: BrowserSession) -> dict:
    """按 Web 登录页顺序在 providers 之后读取一次匿名 NextAuth session。"""
    url = "https://chatgpt.com/api/auth/session"
    headers = session.get_nextauth_headers(referer="https://chatgpt.com/auth/login")
    logger.info("[步骤1.5] 读取匿名 Auth Session...")
    resp = session.get(url, headers=headers)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def signin_openai(
    session: BrowserSession,
    csrf_token: str,
    email: str,
    referer: str = "https://chatgpt.com/auth/login",
    screen_hint: str = "login_or_signup",
) -> str:
    """
    步骤3: 发起 OAuth Signin 请求。
    POST https://chatgpt.com/api/auth/signin/openai

    构造 OAuth 授权参数，获取 authorize URL。

    Args:
        session: 浏览器会话
        csrf_token: 从步骤2获取的 CSRF token
        email: 注册邮箱
        referer: 请求 referer；真实浏览器里是 login_with 完整 URL
        screen_hint: 注册链路用 "signup"（对齐 2026-09-17 抓包）；
                     已有账号的重认证/换绑等链路保持默认 login_or_signup。

    Returns:
        authorize_url: auth.openai.com 的授权 URL
    """
    # 2026-09-17 抓包：查询参数顺序 prompt → screen_hint → ext-oai-did →
    # auth_session_logging_id → login_hint。
    query_params = {
        "prompt": "login",
        "screen_hint": screen_hint,
        "ext-oai-did": session.device_id,
        "auth_session_logging_id": session.auth_session_logging_id,
        "login_hint": email,
    }
    url = "https://chatgpt.com/api/auth/signin/openai?" + urlencode(query_params)

    # 构造请求头
    headers = session.get_nextauth_headers(referer=referer)
    headers["content-type"] = "application/x-www-form-urlencoded"
    headers["origin"] = "https://chatgpt.com"

    # 构造请求体
    body = urlencode({
        "callbackUrl": "/",
        "csrfToken": csrf_token,
        "json": "true",
    })

    logger.info(f"[步骤3] 发起 OAuth Signin 请求, 邮箱: {email}")
    resp = _request_with_retry(session, "signin", lambda: session.post(url, headers=headers, data=body))
    resp.raise_for_status()

    data = resp.json()
    authorize_url = data.get("url", "")

    if not authorize_url:
        raise ValueError(f"[步骤3] 未获取到 authorize URL, 响应: {data}")

    authorize_url = _ensure_authorize_context(authorize_url, session, email, screen_hint=screen_hint)
    logger.info("[步骤3] 获取 authorize URL 成功，已确认 %s/oai-did 上下文", screen_hint)
    logger.debug(f"[步骤3] URL: {authorize_url[:160]}...")
    return authorize_url
