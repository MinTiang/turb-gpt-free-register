# -*- coding: utf-8 -*-
"""注册后对话预热:用协议方式发送真实对话消息。

2026-09-19 存活裸号(capture-exp)分析:账号注册后 ~50s 即发出第一条对话,
随后 01:19/01:54/06:39/13:53 持续使用——"注册即闲置"是协议号被事后清理的
主要嫌疑特征。本模块用已有 sentinel 基建(chat-requirements prepare +
sentinel runner 解 turnstile/pow)协议化复刻这一使用行为。

链路(与抓包逐项对齐):
  POST /backend-api/sentinel/chat-requirements/prepare {"p":...} → prepare_token
  POST sentinel.openai.com/backend-api/sentinel/req (flow=conversation) → challenge
  → runner 解出 turnstile/proof
  POST /backend-api/f/conversation/prepare → conduit_token
  POST /backend-api/f/conversation (SSE) 发送消息并读首块
"""
from __future__ import annotations

import json
import logging
import time
import uuid

from curl_cffi.requests import Session as CurlSession

logger = logging.getLogger(__name__)

_WARMUP_PROMPTS = [
    "hello, what can you do?",
    "hi there! give me a fun fact",
    "hey, how does photosynthesis work?",
    "what's the weather like on mars?",
    "can you recommend a good book?",
]


_ANNOUNCEMENTS = [
    "oai%2Fapps%2FhasSeenOnboarding",
    "oai%2Fapps%2FhasSeenOnboardingFlow",
]


def send_light_warmup(session, access_token: str) -> dict:
    """轻量使用阶梯(2026-09-20 实测全部可用,无 sentinel 依赖)。

    conversation/init(capture 对齐:登录后最先出现)+ announcement_viewed
    + 两个读端点——构成"该账号注册后被正常使用"的服务端可见信号。
    """
    bearer = access_token if access_token.lower().startswith("bearer ") else f"Bearer {access_token}"
    steps = []

    def _h(accept="*/*"):
        h = session.get_chatgpt_headers(referer="https://chatgpt.com/")
        h["authorization"] = bearer
        h["accept"] = accept
        return h

    import time as _t
    try:
        r = session.post(
            "https://chatgpt.com/backend-api/conversation/init",
            headers=_h(), data="{}", timeout=30,
        )
        steps.append(("conversation/init", r.status_code))
    except Exception as exc:
        steps.append(("conversation/init", f"err:{str(exc)[:60]}"))
    _t.sleep(1.2)
    for ann in _ANNOUNCEMENTS:
        try:
            r = session.post(
                f"https://chatgpt.com/backend-api/settings/announcement_viewed?announcement_id={ann}",
                headers=_h(), data="", timeout=20,
            )
            steps.append((f"announcement:{ann[-20:]}", r.status_code))
        except Exception as exc:
            steps.append(("announcement", f"err:{str(exc)[:40]}"))
        _t.sleep(0.8)
    for path in ("/backend-api/models?iim=false&is_gizmo=false", "/backend-api/conversations?offset=0&limit=5"):
        try:
            r = session.get(f"https://chatgpt.com{path}", headers=_h(), timeout=20)
            steps.append((f"get:{path.split('?')[0][-20:]}", r.status_code))
        except Exception as exc:
            steps.append(("get", f"err:{str(exc)[:40]}"))
        _t.sleep(1.0)
    ok = any(code == 200 for _, code in steps if isinstance(code, int))
    logger.info("[Warmup] 轻使用阶梯: %s", steps)
    return {"ok": ok, "status": "light", "steps": [list(x) for x in steps]}


def send_warmup_message(
    session,
    access_token: str,
    *,
    prompt: str | None = None,
    proxy: str | None = None,
) -> dict:
    """完整预热 = 轻阶梯 + f/conversation 对话(尽力而为)。

    f/conversation 对协议会话目前稳定 403(反滥用;turnstile VM 解不被接受
    或缺网页会话 cookie,2026-09-20 实测),失败不影响轻阶梯已产生的使用信号。
    """
    light = send_light_warmup(session, access_token)
    convo = _send_conversation(session, access_token, prompt)
    if convo.get("ok"):
        return {"ok": True, "status": "light+conversation", "light": light.get("steps"), "conversation": "ok"}
    return {"ok": light.get("ok", False), "status": "light_only" if light.get("ok") else "failed",
            "light": light.get("steps"), "conversation_error": (convo.get("detail") or "")[:120]}


def _send_conversation(session, access_token: str, prompt: str | None = None) -> dict:
    if prompt is None:
        import random
        prompt = random.choice(_WARMUP_PROMPTS)

    bearer = access_token if access_token.lower().startswith("bearer ") else f"Bearer {access_token}"
    referer = "https://chatgpt.com/"

    def _headers():
        h = session.get_chatgpt_headers(referer=referer)
        h["authorization"] = bearer
        h["accept"] = "text/event-stream"
        return h

    try:
        # 1. chat-requirements prepare(p 与会话画像一致)
        from core.sentinel import generate_requirements_token
        p = generate_requirements_token(session.sentinel_sid, profile=session.browser_profile)
        resp = session.post(
            "https://chatgpt.com/backend-api/sentinel/chat-requirements/prepare",
            headers=_headers(),
            data=json.dumps({"p": p}, separators=(",", ":")),
            timeout=30,
        )
        if resp.status_code != 200:
            return {"ok": False, "status": "prepare_failed", "detail": f"HTTP {resp.status_code}: {(resp.text or '')[:160]}"}
        req_data = resp.json()
        prepare_token = str(req_data.get("prepare_token") or req_data.get("token") or "")
        if not prepare_token:
            return {"ok": False, "status": "prepare_no_token", "detail": str(req_data)[:200]}
        logger.info("[Warmup] chat-requirements prepare OK, requirements=%s",
                    [k for k, v in req_data.items() if isinstance(v, dict) and v.get("required")])

        # 2. 解 prepare 响应自带的 turnstile/pow challenge(capture 对齐:
        # f/conversation 的 turnstile/proof 来自 chat-requirements prepare,不是 /req)
        from core.openai_auth import build_sentinel_header
        prepare_challenge = {
            "token": prepare_token,
            "turnstile": req_data.get("turnstile") or {},
            "proofofwork": req_data.get("proofofwork") or {},
            "so": req_data.get("so") or {},
        }
        sentinel_header, so_header = build_sentinel_header(session, prepare_challenge, "conversation")
        tokens = json.loads(sentinel_header)  # {p,t,c,flow}

        # 3. f/conversation/prepare → conduit_token
        prep_headers = _headers()
        prep_headers["accept"] = "*/*"
        prep_headers["content-type"] = "application/json"
        prep2 = session.post(
            "https://chatgpt.com/backend-api/f/conversation/prepare",
            headers=prep_headers,
            data="{}",
            timeout=30,
        )
        conduit = ""
        if prep2.status_code == 200:
            try:
                conduit = str(prep2.json().get("conduit_token") or "")
            except Exception:
                conduit = ""

        # 4. f/conversation SSE 发送
        headers = _headers()
        headers.update({
            "openai-sentinel-chat-requirements-token": prepare_token,
            "openai-sentinel-turnstile-token": tokens.get("t") or "",
            "openai-sentinel-proof-token": tokens.get("p") or "",
            "x-oai-turn-trace-id": str(uuid.uuid4()),
        })
        if conduit:
            headers["x-conduit-token"] = conduit
        if so_header:
            headers["openai-sentinel-so-token"] = so_header
        body = {
            "action": "next",
            "messages": [{
                "id": str(uuid.uuid4()),
                "author": {"role": "user"},
                "create_time": round(time.time(), 2),
                "content": {"content_type": "text", "parts": [prompt]},
                "metadata": {"serialization_metadata": {"custom_symbol_offsets": []}, "submission_mode": "manual_send"},
            }],
            "parent_message_id": "client-created-root",
            "model": "auto",
            "client_prepare_state": "success" if conduit else "none",
            "timezone_offset_min": session.js_timezone_offset_min(),
            "timezone": str((session.browser_profile or {}).get("timezone_iana") or "UTC"),
            "conversation_mode": {"kind": "primary_assistant"},
            "enable_message_followups": True,
            "system_hints": [],
            "supports_buffering": True,
            "supported_encodings": ["v1"],
            "client_contextual_info": {"is_dark_mode": False, "time_since_loaded": 30, "page_height": 900, "page_width": 1440},
        }
        conv = session.post(
            "https://chatgpt.com/backend-api/f/conversation",
            headers=headers,
            data=json.dumps(body, separators=(",", ":")),
            timeout=60,
        )
        if conv.status_code != 200:
            return {"ok": False, "status": "conversation_failed", "detail": f"HTTP {conv.status_code}: {(conv.text or '')[:200]}"}
        # SSE 读首块即可(拿到响应即代表消息被受理)
        first = (conv.text or "")[:120]
        logger.info("[Warmup] 对话已发送并收到响应: %s...", first.replace(chr(10), ' ')[:80])
        return {"ok": True, "status": "sent", "detail": {"prompt": prompt, "first_chunk": first[:80]}}
    except Exception as exc:
        logger.warning("[Warmup] 对话预热失败: %s: %s", type(exc).__name__, str(exc)[:200])
        return {"ok": False, "status": "exception", "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}


def warmup_account(email: str, access_token: str, *, proxy: str | None = None, messages: int = 1) -> list[dict]:
    """为已注册账号做使用预热:复用注册时的设备身份(同 device_id/profile)发消息。

    注册后 60s 内冒出"陌生设备"聊天会触发 Unusual activity(2026-09-20 实测),
    存活浏览器样本是同一设备持续使用——这里从账号 extra_json 恢复注册会话身份。
    """
    from core.session import BrowserSession
    identity = _load_registration_identity(email)
    results = []
    for i in range(max(1, messages)):
        s = BrowserSession(
            proxy=proxy if proxy is not None else "",
            detect_exit_geo=False,
            device_id=identity.get("device_id"),
            oai_session_id=identity.get("oai_session_id"),
            sentinel_sid=identity.get("sentinel_sid"),
            auth_session_logging_id=identity.get("auth_session_logging_id"),
            browser_profile=identity.get("browser_profile") or None,
        )
        try:
            r = send_warmup_message(s, access_token)
            results.append(r)
            logger.info("[Warmup][%s] 第 %s/%s 条: %s", email, i + 1, messages, r.get("status"))
        finally:
            try:
                s.session.close()
            except Exception:
                pass
        if i < messages - 1:
            time.sleep(__import__("random").uniform(20, 60))
    return results


def _load_registration_identity(email: str) -> dict:
    """从账号 extra_json 恢复注册会话的设备身份(缺失字段返回 None 由会话随机)。"""
    try:
        from core import db
        account = db.get_account_by_email(email) or {}
        extra = {}
        raw = account.get("extra_json") or account.get("payload") or ""
        if isinstance(raw, str) and raw:
            extra = json.loads(raw)
            if isinstance(extra.get("extra_json"), str):
                extra = json.loads(extra.get("extra_json") or "{}")
        profile = extra.get("browser_profile")
        return {
            "device_id": extra.get("device_id"),
            "oai_session_id": extra.get("oai_session_id"),
            "sentinel_sid": extra.get("sentinel_sid"),
            "auth_session_logging_id": extra.get("auth_session_logging_id"),
            "browser_profile": profile if isinstance(profile, dict) else None,
        }
    except Exception as exc:
        logger.debug("[Warmup] 恢复注册身份失败(用随机身份): %s", exc)
        return {}
