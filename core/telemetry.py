# -*- coding: utf-8 -*-
"""注册会话前端遥测复刻(2026-09-18 真浏览器抓包)。

真浏览器注册时前端会发出三类信号,纯协议注册原先全部缺失——
账号注册接口全放行、但随后被近乎实时清理的主要嫌疑特征:

1. mweb events   : chatgpt.com/unauth-mweb/events/*(页面浏览上报)
2. CES / statsig : chatgpt.com/ces/v1/*、ab.chatgpt.com/v1/initialize
                   (Segment/Statsig 埋点;initialize 请求体为倒序 base64)
3. Datadog RUM   : auth.openai.com/awe/api/v2/rum(视图/性能事件,NDJSON 批)

全部 best-effort:任何失败只记 debug 日志,绝不影响注册主流程。
"""
from __future__ import annotations

import base64
import json
import logging
import random
import re
import time
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Datadog RUM(login-web 服务)固定参数,来自抓包。
_DD_APP_ID = "a10b421a-4c98-4e3a-9b20-89ebbf54befa"
_DD_API_KEY = "pub87c677d9c9bf34b387f60a689c1990a6"
_DD_SDK_VERSION = "6.4.0"
_AUTH_WEB_VERSION_DEFAULT = "d7879423bb23cb29e7de5c45a16f35fd563fa23e"
_AB_SDK_VERSION = "3.32.7"
_OAUTH_CLIENT_ID = "app_X8zY6vW2pQ9tR3dE7nK1jL5gH"

# auth.openai.com 登录/注册各路由的页面元数据(抓包实测)。
AUTH_ROUTES = {
    "create-account/password": ("CREATE_ACCOUNT_PASSWORD", "Create Account Password", "创建密码 - OpenAI"),
    "email-verification": ("EMAIL_VERIFICATION", "Email Verification", "检查你的收件箱 - OpenAI"),
    "about-you": ("ABOUT_YOU", "About You", "确认一下你的年龄 - OpenAI"),
    "log-in/password": ("LOG_IN_PASSWORD", "Log In Password", "输入密码 - OpenAI"),
    "log-in-or-create-account": ("UNIFIED_LOG_IN_OR_SIGN_UP_INPUT", "Unified Login Or Signup Input Page", "登录或注册 - OpenAI"),
    "add-phone": ("ADD_PHONE", "Add Phone", "电话号码是必填项 - OpenAI"),
}


def _iso_z(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _reverse_b64(obj) -> str:
    """statsig 客户端的 initialize 请求体编码:base64(JSON) 后整串倒序。"""
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")[::-1]


class TelemetryEmitter:
    """单个注册会话的前端遥测发射器。与 BrowserSession 一一对应。"""

    def __init__(self, session, *, enabled: bool = True):
        self.session = session
        self.enabled = bool(enabled)
        self.anonymous_id = str(uuid.uuid4())
        self.statsig_sid = str(uuid.uuid4())
        self.oaicom_stable_id = str(uuid.uuid4())
        self.rum_sid = str(uuid.uuid4())
        self.auth_version = _AUTH_WEB_VERSION_DEFAULT
        self._last_auth_page = "https://auth.openai.com/"

    def observe_auth_version(self, html: str) -> bool:
        """从授权页 HTML 顺带同步 auth-web 前端版本号(RUM/事件的 appVersion)。"""
        match = re.search(r'"version"\s*:\s*"([0-9a-f]{40})"', str(html or ""))
        if not match:
            return False
        self.auth_version = match.group(1)
        return True

    # ---------- 上下文构造(抓包同构) ----------

    def _ua(self) -> str:
        profile = getattr(self.session, "browser_profile", {}) or {}
        return str(profile.get("user_agent") or "")

    def _ua_data(self) -> dict:
        profile = getattr(self.session, "browser_profile", {}) or {}
        major = str(profile.get("chrome_major") or "153")
        return {
            "brands": [
                {"brand": "Microsoft Edge", "version": major},
                {"brand": "Not_A Brand", "version": "8"},
                {"brand": "Chromium", "version": major},
            ],
            "mobile": False,
            "platform": str(profile.get("user_agent_data_platform") or "Windows"),
        }

    def _statsig_user(self, route_path: str = "") -> dict:
        s = self.session
        geo = getattr(s, "exit_geo", {}) or {}
        did = s.device_id
        return {
            "locale": s.navigator_language(),
            "ip": geo.get("ip") or "",
            "country": geo.get("country") or "",
            "appVersion": self.auth_version,
            "userAgent": self._ua(),
            "customIDs": {
                "WebAnonymousCookieID": did,
                "DeviceId": did,
                "stableID": did,
                "oaicom_stable_id": self.oaicom_stable_id,
                "source_surface_stable_id": did,
                "AuthSessionLoggingId": s.auth_session_logging_id,
                "oauth_client_id": _OAUTH_CLIENT_ID,
            },
            "custom": {
                "client_id": _OAUTH_CLIENT_ID,
                "app_name_enum": "chat",
                "originator": "",
                "AuthSessionLoggingId": s.auth_session_logging_id,
                "client_type": "web",
                "route": route_path,
            },
            "statsigEnvironment": {"tier": "production"},
            "analyticsOnlyMetadata": {
                "$s_referrer": "https://chatgpt.com/",
                "$s_referrer_domain": "chatgpt.com",
                "$s_referrer_path": "/",
            },
        }

    def _ces_url(self, path: str, *, ec: int | None = None) -> str:
        from config import AB_CLIENT_KEY
        url = (
            f"https://chatgpt.com/ces/v1/{path}"
            f"?k={AB_CLIENT_KEY}&st=javascript-client&sv={_AB_SDK_VERSION}"
            f"&t={int(time.time() * 1000)}&sid={self.statsig_sid}"
        )
        if ec is not None:
            url += f"&ec={ec}"
        return url

    def _segment_context(self, page: dict) -> dict:
        return {
            "page": page,
            "userAgent": self._ua(),
            "userAgentData": self._ua_data(),
            "locale": self.session.navigator_language(),
            "library": {"name": "analytics.js", "version": "5.2.0"},
        }

    def _rum_url(self) -> str:
        from urllib.parse import quote
        tags = quote(f"sdk_version:{_DD_SDK_VERSION},api:fetch,env:prod,service:login-web,version:{self.auth_version}", safe="")
        return (
            f"https://auth.openai.com/awe/api/v2/rum"
            f"?ddsource=browser&ddtags={tags}&dd-api-key={_DD_API_KEY}"
            f"&dd-evp-origin-version={_DD_SDK_VERSION}&dd-evp-origin=browser"
            f"&dd-request-id={uuid.uuid4()}&batch_time={int(time.time() * 1000)}"
        )

    def _rum_event(self, *, route_id: str, view_url: str, view_name: str, kind: str = "vital") -> dict:
        s = self.session
        now = time.time() * 1000
        return {
            "_dd": {
                "format_version": 2,
                "drift": 0,
                "configuration": {"session_sample_rate": 100, "session_replay_sample_rate": 1},
                "vital": {"computed_value": True},
            },
            "application": {"id": _DD_APP_ID},
            "date": int(now),
            "source": "browser",
            "session": {"id": self.rum_sid, "type": "user"},
            "connectivity": {"status": "connected", "effective_type": "3g"},
            "context": {
                "clientId": _OAUTH_CLIENT_ID,
                "appNameEnum": "chat",
                "sessionLoggingId": s.auth_session_logging_id,
                "track": "stable",
                "deviceId": s.device_id,
                "routeId": route_id,
                "isError": False,
            },
            "type": kind,
            "view": {
                "url": view_url,
                "referrer": "https://chatgpt.com/",
                "id": str(uuid.uuid4()),
                "name": view_name,
            },
            "service": "login-web",
            "version": self.auth_version,
        }

    # ---------- 发送(全部 best-effort) ----------

    def _send(self, url: str, body: str, *, referer: str, skip_target_headers: bool = False) -> None:
        if not self.enabled:
            return
        try:
            headers = self.session._get_common_headers()
            headers["content-type"] = "text/plain;charset=UTF-8"
            if referer:
                headers["referer"] = referer
            resp = self.session.post(
                url,
                headers=headers,
                data=body.encode("utf-8"),
                skip_target_headers=skip_target_headers,
                timeout=15,
            )
            status = int(getattr(resp, "status_code", 0) or 0)
            if status in (403, 429):
                # 遥测请求被 CF 拦截:立即永久停用本会话遥测,并恢复会话状态,
                # 绝不让可选遥测的质询污染污染后续注册主链路。
                self.enabled = False
                reset = getattr(self.session, "reset_circuit_breaker", None)
                if callable(reset):
                    reset()
                rebuild = getattr(self.session, "rebuild_transport", None)
                if callable(rebuild):
                    try:
                        rebuild()
                    except Exception:
                        pass
                logger.warning("[遥测] 遥测请求被质询(HTTP %s),已停用遥测并恢复会话", status)
        except Exception as exc:
            logger.debug("[遥测] 发送失败 %s: %s: %s", url[:80], type(exc).__name__, str(exc)[:120])

    # ---------- 事件 ----------

    def emit_homepage(self) -> None:
        """首页加载遥测:mweb page-view + CES page + statsig initialize。"""
        if not self.enabled:
            return
        now = time.time() * 1000
        page_view = {
            "anonymousId": self.anonymous_id,
            "isDocumentEntry": True,
            "occurredAt": _iso_z(now),
            "referrer": "",
            "url": "https://chatgpt.com/",
            "appEntryInstanceId": str(uuid.uuid4()),
            "entryContext": {
                "schemaVersion": 1,
                "capturedAt": _iso_z(now - random.uniform(1500, 3000)),
                "webSurface": "lightweight_web",
                "pathname": "/",
                "clickIdParameterNames": [],
            },
            "entryUrl": "https://chatgpt.com/",
        }
        self._send(
            "https://chatgpt.com/unauth-mweb/events/page-view",
            json.dumps(page_view, ensure_ascii=False, separators=(",", ":")),
            referer="https://chatgpt.com/",
        )

        page = {
            "path": "/", "referrer": "", "search": "",
            "title": "ChatGPT", "url": "https://chatgpt.com/", "hash": "/",
        }
        ces_page = {
            "timestamp": _iso_z(now),
            "integrations": {"Segment.io": True},
            "type": "page",
            "properties": {**page, "origin": "chat"},
            "context": self._segment_context(page),
        }
        self._send(
            self._ces_url("p"),
            json.dumps(ces_page, ensure_ascii=False, separators=(",", ":")),
            referer="https://chatgpt.com/",
            skip_target_headers=True,
        )

        init_user = self._statsig_user("/create-account/password")
        init_payload = {
            "user": init_user,
            "hash": "djb2",
            "deltasResponseRequested": True,
            "sinceTime": 0,
            "previousDerivedFields": {},
            "partialUserMatchSinceTime": int(now - random.uniform(60000, 600000)),
            "statsigMetadata": {
                "sdkVersion": _AB_SDK_VERSION,
                "sdkType": "javascript-client",
                "stableID": self.session.device_id,
                "sessionID": self.statsig_sid,
                "fallbackUrl": None,
            },
        }
        self._send(
            f"https://ab.chatgpt.com/v1/initialize?k="
            + self._ab_key()
            + f"&st=javascript-client&sv={_AB_SDK_VERSION}"
            + f"&t={int(now)}&sid={self.statsig_sid}&se=1",
            _reverse_b64(init_payload),
            referer="https://chatgpt.com/",
        )

    @staticmethod
    def _ab_key() -> str:
        from config import AB_CLIENT_KEY
        return AB_CLIENT_KEY

    def emit_auth_view(self, landing_url: str) -> None:
        """按 authorize 落点路由发送 auth 页面遥测(CES 批 + RUM 视图 + rgstr)。"""
        if not self.enabled:
            return
        route_key = next(
            (key for key in AUTH_ROUTES if key in str(landing_url or "")), None
        )
        if not route_key:
            return
        route_id, name, title = AUTH_ROUTES[route_key]
        page_url = f"https://auth.openai.com/{route_key}"
        self._last_auth_page = page_url
        now = time.time() * 1000

        page = {
            "path": f"/{route_key}",
            "referrer": "https://chatgpt.com/",
            "search": "",
            "title": title,
            "url": page_url,
            "route_id": route_id,
            "is_error": False,
            "origin": "login-web",
            "category": "Identity",
            "name": name,
        }
        batch_event = {
            "timestamp": _iso_z(now),
            "integrations": {"Segment.io": True},
            "type": "page",
            "properties": page,
            "category": "Identity",
            "name": name,
            "context": self._segment_context({k: page[k] for k in ("path", "referrer", "search", "title", "url")}),
        }
        self._send(
            self._ces_url("b"),
            json.dumps({"writeKey": "oai", "batch": [batch_event]}, ensure_ascii=False, separators=(",", ":")),
            referer=page_url,
            skip_target_headers=True,
        )

        events = [
            self._rum_event(route_id=route_id, view_url=page_url, view_name=name)
            for _ in range(random.randint(2, 3))
        ]
        rum_body = "\n".join(json.dumps(ev, ensure_ascii=False, separators=(",", ":")) for ev in events)
        self._send(self._rum_url(), rum_body, referer=page_url)

        rgstr_event = {
            "eventName": "auto_capture::performance",
            "value": page_url,
            "metadata": {
                "sessionID": self.statsig_sid,
                "page_url": page_url,
                "load_time_ms": 0,
                "dom_interactive_time_ms": round(random.uniform(1500, 4500), 3),
                "redirect_count": 1,
                "transfer_bytes": random.randint(15000, 25000),
                "first_contentful_paint_time_ms": round(random.uniform(1500, 4500), 3),
                "effective_connection_type": "3g",
                "rtt_ms": random.randint(200, 700),
                "downlink_mbps": round(random.uniform(0.8, 2.5), 2),
                "downlink_kbps": random.randint(800, 2500),
                "save_data": False,
            },
            "user": self._statsig_user(f"/{route_key}"),
        }
        rgstr_body = {"events": [rgstr_event], "statsigMetadata": {
            "sdkVersion": _AB_SDK_VERSION,
            "sdkType": "javascript-client",
            "stableID": self.session.device_id,
            "sessionID": self.statsig_sid,
            "fallbackUrl": None,
        }}
        self._send(
            self._ces_url("rgstr", ec=1),
            json.dumps(rgstr_body, ensure_ascii=False, separators=(",", ":")),
            referer=page_url,
            skip_target_headers=True,
        )
        logger.info("[遥测] auth 页面视图已上报:%s", route_id)
