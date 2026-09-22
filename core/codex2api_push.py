# -*- coding: utf-8 -*-
"""Codex 授权结果推送到 codex2api。

codex2api 的账号接口是 POST /api/admin/accounts/import，body 为
`{"accounts": [{...}]}` 或裸条目数组，条目字段见 admin/handler.go 的
jsonAccountEntry。本模块只负责把 turb 拿到的 OAuth 凭证整理成它认的字段。

两种授权来源的差别（必须区分，否则续期失败）：

    接码授权  CODEX_OAUTH_DRIVER=protocol/cloak 走 Codex CLI 客户端
              client_id = app_EMoamEEZ73f0CkXaXp7hrann
    平台授权  CODEX_OAUTH_DRIVER=platform 走官网 platform.openai.com 客户端
              client_id = app_2SKx67EdpoN0G6j64rFvigXD

OpenAI 的 refresh_token 绑定签发它的 client，用错 client 刷新会返回
401 invalid_client（实测 2026-09-22）。因此推送时一律带上 client_id，
codex2api 落库到 credentials.oauth_client_id，刷新时按它选 client。
"""
from __future__ import annotations

import json
import logging
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

# 两种授权的 client（与 codex2api auth 包中的常量一致）
CLI_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
PLATFORM_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"

# 授权来源标签 → client_id。codex2api 侧只认这两个 client。
_DRIVER_CLIENT_ID = {
    "platform": PLATFORM_CLIENT_ID,
    "protocol": CLI_CLIENT_ID,
    "cloak": CLI_CLIENT_ID,
    "browse": CLI_CLIENT_ID,
}


def resolve_client_id(driver: str = "", payload: dict | None = None) -> str:
    """决定本次推送该带哪个 client_id。

    优先用驱动名映射；驱动名未知时回退到 payload 里已有的 client_id 或
    id_token 的 JWT 声明；都拿不到就返回空串（codex2api 侧会退回 CLI client）。
    """
    key = str(driver or "").strip().lower()
    if key in _DRIVER_CLIENT_ID:
        return _DRIVER_CLIENT_ID[key]
    data = payload or {}
    explicit = str(data.get("client_id") or "").strip()
    if explicit in (CLI_CLIENT_ID, PLATFORM_CLIENT_ID):
        return explicit
    # 从 id_token 的 JWT 声明反推（aud / client_id / azp）
    id_token = str(data.get("id_token") or "").strip()
    if id_token:
        detected = _detect_client_id_from_jwt(id_token)
        if detected:
            return detected
    return ""


def _detect_client_id_from_jwt(token: str) -> str:
    """从 JWT 的 aud / client_id / azp 声明里认出签发 client。"""
    try:
        import base64
        parts = str(token).split(".")
        if len(parts) < 2:
            return ""
        seg = parts[1]
        seg += "=" * (-len(seg) % 4)
        claims = json.loads(base64.urlsafe_b64decode(seg))
    except Exception:
        return ""
    if not isinstance(claims, dict):
        return ""
    candidates = []
    aud = claims.get("aud")
    if isinstance(aud, str):
        candidates.append(aud)
    elif isinstance(aud, list):
        candidates.extend(str(x) for x in aud)
    for field in ("client_id", "azp"):
        value = claims.get(field)
        if isinstance(value, str):
            candidates.append(value)
    for value in candidates:
        if value in (CLI_CLIENT_ID, PLATFORM_CLIENT_ID):
            return value
    return ""


def _codex2api_origin() -> str:
    from config import codex as _cfg
    raw = str(getattr(_cfg, "CODEX2API_URL", "") or "").strip()
    if not raw:
        raise RuntimeError("[Codex][Push] 尚未配置 CODEX2API_URL")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError(f"[Codex][Push] CODEX2API_URL 格式无效: {raw}")
    return f"{parsed.scheme}://{parsed.netloc}"


def _codex2api_key() -> str:
    from config import codex as _cfg
    key = str(getattr(_cfg, "CODEX2API_ADMIN_KEY", "") or "").strip()
    if not key:
        raise RuntimeError("[Codex][Push] 尚未配置 CODEX2API_ADMIN_KEY")
    return key


def build_import_entry(payload: dict, driver: str = "") -> dict:
    """把 turb 的 OAuth 凭证整理成 codex2api 的导入条目。

    只带 codex2api 认的字段；client_id 是区分两种授权的关键，
    接码授权与平台授权各自写自己的 client。
    """
    data = dict(payload or {})
    email = str(data.get("email") or "").strip()
    entry = {
        "name": email or str(data.get("account_id") or "codex"),
        "email": email,
        "account_id": str(data.get("account_id") or "").strip(),
        "access_token": str(data.get("access_token") or "").strip(),
        "refresh_token": str(data.get("refresh_token") or "").strip(),
        "id_token": str(data.get("id_token") or "").strip(),
    }
    client_id = resolve_client_id(driver, data)
    if client_id:
        entry["client_id"] = client_id
    if data.get("expired"):
        entry["expires_at"] = data["expired"]
    if data.get("last_refresh"):
        entry["last_refresh"] = data["last_refresh"]
    return {k: v for k, v in entry.items() if v not in ("", None)}


def push_to_codex2api(payload: dict, driver: str = "", entry: dict | None = None) -> dict:
    """推送单个账号到 codex2api。

    driver 决定 client_id（platform → 官网 client；其余 → Codex CLI client）。
    失败抛异常，由调用方决定是否影响主流程。
    """
    origin = _codex2api_origin()
    key = _codex2api_key()
    item = entry if entry is not None else build_import_entry(payload, driver)
    if not item.get("refresh_token") and not item.get("access_token"):
        raise RuntimeError("[Codex][Push] 凭证缺少 refresh_token 与 access_token，拒绝推送")
    # /api/admin/accounts/import 收 multipart：format=json + file 字段（见
    # admin/handler.go importAccountsJSON）。条目字段用 jsonAccountEntry 的键。
    from config import codex as _cfg
    timeout = int(getattr(_cfg, "CODEX2API_PUSH_TIMEOUT", 30) or 30)
    email = item.get("email") or item.get("name") or "codex"
    safe = "".join(ch if (ch.isalnum() or ch in "@._-") else "-" for ch in str(email)).strip("-._") or "codex"
    filename = f"codex-{safe}.json"
    body = json.dumps(item, ensure_ascii=False).encode("utf-8")
    logger.info(
        "[Codex][Push] 推送账号到 codex2api: %s (client_id=%s, 来源=%s)",
        email,
        item.get("client_id") or "未指定(由服务端推断)",
        driver or "unknown",
    )
    resp = requests.post(
        f"{origin}/api/admin/accounts/import",
        headers={
            "Authorization": f"Bearer {key}",
            "X-Admin-Key": key,
        },
        data={"format": "json"},
        files={"file": (filename, body, "application/json")},
        timeout=timeout,
    )
    if not (200 <= int(resp.status_code) < 300):
        raise RuntimeError(
            f"[Codex][Push] codex2api 返回 {resp.status_code}: {(resp.text or '')[:300]}"
        )
    try:
        result = resp.json()
    except Exception:
        result = {}
    logger.info("[Codex][Push] 推送成功: %s", item.get("email") or item.get("name"))
    return result if isinstance(result, dict) else {}
