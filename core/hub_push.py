# -*- coding: utf-8 -*-
"""gpt-account-hub 推送服务。

把本地账号(注册产物)按 hub 的接收契约打包推送:
POST {HUB_PUSH_BASE_URL}/api/accounts/import
body = {"source":"push","account":{
    email, access_token, password, totp_secret, plan_type,
    mailbox{email,password,client_id,refresh_token},
    credentials[{kind,value}...],   # codex_access/codex_refresh/codex_id
    registered_channel }}           # protocol/browse/manual(有效时才携带)

hub 必填校验:email + access_token + mailbox 四件套;outlook 来源账号天然满足,
其他邮箱源会被 hub 拒收并在结果里说明。注册出口 IP 已按要求不再推送。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# turb 通道名 → hub registered_channel 枚举(2026-09-20 与 hub 对齐:
# hub 只认 protocol/browse/manual,browser 系驱动统一归 browse)
_CHANNEL_ALIASES = {
    "protocol": "protocol",
    "browse": "browse",
    "manual": "manual",
    # 历史驱动名兼容归一
    "cloak": "browse",
    "cloakbrowser": "browse",
}


def _cfg():
    from config import hub_push as cfg
    return cfg


def _find_codex_file(email: str) -> Path | None:
    """找本地 codex 授权产物(codex-邮箱*.json),取最新的一份。"""
    try:
        from config import codex as codex_cfg
        root = Path(getattr(codex_cfg, "CODEX_LOCAL_DIR", "") or "codex_accounts")
        if not root.is_absolute():
            root = Path(__file__).resolve().parent.parent / root
        if not root.exists():
            return None
        candidates = sorted(
            [p for p in root.glob(f"codex-{email}*.json")],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return candidates[0] if candidates else None
    except Exception:
        return None


def build_hub_payload(email: str) -> dict:
    """从本地 DB(+codex 文件)构造 hub 推送 payload。抛错说明素材不齐。"""
    from core import db

    account = db.get_account_by_email(email)
    if not account:
        raise RuntimeError(f"本地账号不存在: {email}")

    access_token = str(account.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError(f"账号没有 accessToken,无法推送: {email}")

    # mailbox 四件套:outlook 池行(注册时消费的原始素材)
    mailbox = None
    outlook = db.get_outlook_by_email(email)
    if outlook:
        mailbox = {
            "email": str(outlook.get("email") or email),
            "password": str(outlook.get("password") or ""),
            "client_id": str(outlook.get("client_id") or ""),
            "refresh_token": str(outlook.get("refresh_token") or ""),
        }
    if not mailbox or not all(mailbox.values()):
        raise RuntimeError(
            f"邮箱四件套不完整(hub 必填): {email};"
            "仅 outlook 池来源账号可推送"
        )

    # 注册密码 / totp / 注册通道 从 extra_json 恢复
    password = ""
    totp_secret = ""
    registration_channel = ""
    try:
        extra = json.loads(str(account.get("extra_json") or "{}"))
        if isinstance(extra, dict):
            password = str(extra.get("registration_password") or "")
            totp_secret = str(extra.get("totp_secret") or "")
            registration_channel = str(extra.get("registration_channel") or "")
    except Exception:
        pass
    # 通道归一到 hub 枚举(protocol/browse/manual):browser 系驱动统一 browse
    registration_channel = _CHANNEL_ALIASES.get(registration_channel.strip().lower(), "")

    # codex 令牌:本地授权产物存在时附带
    credentials = []
    codex_file = _find_codex_file(email)
    if codex_file is not None:
        try:
            data = json.loads(codex_file.read_text(encoding="utf-8"))
            for kind, key in (
                ("codex_access", "access_token"),
                ("codex_refresh", "refresh_token"),
                ("codex_id", "id_token"),
            ):
                value = str(data.get(key) or "").strip()
                if value:
                    credentials.append({"kind": kind, "value": value})
            logger.info("[HubPush] 附带 codex 令牌 %s 项(%s)", len(credentials), codex_file.name)
        except Exception as exc:
            logger.warning("[HubPush] codex 文件解析失败(跳过附带): %s: %s", codex_file.name, exc)

    account_payload = {
        "email": email,
        "access_token": access_token,
        "password": password,
        "totp_secret": totp_secret,
        "plan_type": str(account.get("plan_type") or "free"),
        "mailbox": mailbox,
    }
    # 注册通道(hub 枚举 protocol/browse/manual):仅在有效时携带,避免空串被
    # hub 的 Literal 校验拒掉整个推送。registered_ip 已按要求不再推送。
    if registration_channel:
        account_payload["registered_channel"] = registration_channel
    if credentials:
        account_payload["credentials"] = credentials
    return {"source": "push", "account": account_payload}


def push_account_to_hub(email: str, *, reason: str = "manual") -> dict:
    """推送单个账号到 hub。返回 {ok, status, message, resp?}。"""
    cfg = _cfg()
    base_url = str(cfg.HUB_PUSH_BASE_URL or "").strip().rstrip("/")
    api_key = str(cfg.HUB_PUSH_API_KEY or "").strip()
    if not cfg.HUB_PUSH_ENABLED or not base_url or not api_key:
        return {
            "ok": False,
            "status": "disabled",
            "message": "推送未启用或 HUB_PUSH_BASE_URL/HUB_PUSH_API_KEY 未配置",
        }

    payload = build_hub_payload(email)
    import requests

    url = f"{base_url}/api/accounts/import"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    timeout = max(5, int(cfg.HUB_PUSH_TIMEOUT))
    attempts = max(1, int(cfg.HUB_PUSH_RETRY) + 1)

    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.post(
                url, headers=headers,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=timeout,
            )
            if resp.status_code == 200:
                data = resp.json() if resp.text else {}
                imported = int(data.get("imported") or 0)
                updated = int(data.get("updated") or 0)
                skipped = int(data.get("skipped") or 0)
                if imported:
                    message = f"推送成功(新入库): {email}"
                elif updated:
                    message = f"推送成功(更新已有记录): {email}"
                elif skipped:
                    message = f"推送完成(hub 已存在,跳过): {email}"
                else:
                    message = f"hub 返回 {data}: {email}"
                logger.info("[HubPush][%s] %s", reason, message)
                return {"ok": True, "status": "ok", "message": message, "resp": data}
            # 4xx(除 429)是契约/素材问题,重试无意义
            body = (resp.text or "")[:200]
            last_error = f"HTTP {resp.status_code}: {body}"
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                break
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < attempts:
            time.sleep(1.5 * attempt)

    message = f"推送失败({email}): {last_error}"
    logger.warning("[HubPush][%s] %s", reason, message)
    return {"ok": False, "status": "error", "message": message}


def maybe_push_after_register(email: str) -> dict | None:
    """注册成功后的自动推送钩子;未启用返回 None(不影响注册主流程)。"""
    cfg = _cfg()
    if not cfg.HUB_PUSH_ENABLED or not cfg.HUB_PUSH_ON_REGISTER:
        return None
    try:
        return push_account_to_hub(email, reason="auto")
    except Exception as exc:
        # 自动推送永远不阻断注册结果
        logger.warning("[HubPush][auto] 构造/推送异常(忽略): %s: %s", type(exc).__name__, exc)
        return {"ok": False, "status": "error", "message": f"{type(exc).__name__}: {exc}"}
