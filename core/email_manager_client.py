# -*- coding: utf-8 -*-
"""outlookEmail (assast/outlookEmail) 管理端客户端。

单一事实源方案: 邮箱素材与 refresh_token 只在邮箱管理端保存/刷新,
本项目通过其 API 领取邮箱、取验证码、回写成功/失败, 不再本地持有
refresh_token —— 根治双份 token 轮换互踢导致的失效问题。

鉴权: Web 登录密码换 Session( permanent), 写操作带 X-CSRFToken;
邮件读取走对外 API(X-API-Key) 或 Session 均可, 这里统一用 Session。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)


class ManagerClient:
    def __init__(self, base_url: str, password: str, project_key: str, lease_seconds: int = 900):
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.project_key = project_key
        self.lease_seconds = max(60, min(int(lease_seconds or 900), 3600))
        self._sess = requests.Session()
        self._csrf: str | None = None
        self._lock = threading.Lock()

    # ---- 会话 ----
    def _login(self) -> None:
        r = self._sess.post(
            f"{self.base_url}/login",
            json={"password": self.password, "session_duration_days": "permanent"},
            timeout=20,
        )
        r.raise_for_status()
        r2 = self._sess.get(f"{self.base_url}/api/csrf-token", timeout=20)
        r2.raise_for_status()
        self._csrf = (r2.json() or {}).get("csrf_token") or (r2.json() or {}).get("token")
        logger.info("[邮箱管理端] 登录成功 project=%s", self.project_key)

    def _ensure(self) -> None:
        with self._lock:
            if self._csrf is None:
                self._login()

    def _post(self, path: str, payload: dict | None = None, retry: bool = True) -> dict:
        self._ensure()
        headers = {}
        if self._csrf:
            headers["X-CSRFToken"] = self._csrf
        r = self._sess.post(
            f"{self.base_url}{path}", json=payload or {}, headers=headers, timeout=30
        )
        if r.status_code in (401, 403) and retry:
            self._csrf = None
            self._login()
            return self._post(path, payload, retry=False)
        r.raise_for_status()
        try:
            return r.json() or {}
        except Exception:
            return {}

    def _get(self, path: str, params: dict | None = None, retry: bool = True) -> dict:
        self._ensure()
        r = self._sess.get(f"{self.base_url}{path}", params=params or {}, timeout=30)
        if r.status_code in (401, 403) and retry:
            self._csrf = None
            self._login()
            return self._get(path, params, retry=False)
        r.raise_for_status()
        try:
            return r.json() or {}
        except Exception:
            return {}

    # ---- 项目操作 ----
    def claim_email(self, caller_id: str, task_id: str) -> dict:
        """随机领取一个 toClaim 邮箱。返回 {email, account_id, claim_token, ...}。

        池空时抛 LookupError; 5xx/网络错误自动重试(管理端偶发 500, 实测
        隔几秒重试即成功)。
        """
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                data = self._post(
                    f"/api/projects/{self.project_key}/claim-random",
                    {"caller_id": caller_id, "task_id": task_id, "lease_seconds": self.lease_seconds},
                )
                break
            except Exception as exc:
                last_exc = exc
                logger.warning("[邮箱管理端] claim 第 %s 次失败: %s", attempt + 1, str(exc)[:120])
                time.sleep(3 + attempt * 3)
        else:
            raise RuntimeError(f"claim 重试 4 次仍失败: {last_exc}")
        if not data.get("success"):
            raise LookupError(str(data.get("error") or data.get("message") or "claim 失败"))
        d = data.get("data") or {}
        if not d.get("email"):
            raise LookupError("claim 响应缺少 email")
        return d

    def complete_success(self, account_id: int, claim_token: str, detail: str = "") -> None:
        self._post(
            f"/api/projects/{self.project_key}/complete-success",
            {"account_id": account_id, "claim_token": claim_token,
             "caller_id": "turb", "detail": detail or "注册成功"},
        )

    def complete_failed(self, account_id: int, claim_token: str, detail: str = "") -> None:
        self._post(
            f"/api/projects/{self.project_key}/complete-failed",
            {"account_id": account_id, "claim_token": claim_token,
             "caller_id": "turb", "detail": (detail or "")[:180]},
        )

    def release(self, account_id: int, claim_token: str, detail: str = "") -> None:
        self._post(
            f"/api/projects/{self.project_key}/release",
            {"account_id": account_id, "claim_token": claim_token,
             "caller_id": "turb", "detail": (detail or "")[:180]},
        )

    def ensure_tag(self, name: str, color: str = "#563d7c") -> int:
        """按名字取标签 id, 不存在则创建。"""
        raw = self._get("/api/tags")
        tag_list = raw.get("tags") or (raw.get("data") or {}).get("tags") or []
        for t in tag_list:
            if str(t.get("name") or "") == name:
                return int(t.get("id"))
        r = self._post("/api/tags", {"name": name, "color": color})
        d = r.get("data") or r.get("tag") or r
        return int(d.get("id") or d.get("tag_id"))

    def account_tag(self, account_id: int, tag_id: int, action: str = "add") -> None:
        self._post("/api/accounts/tags",
                   {"account_ids": [account_id], "tag_id": tag_id, "action": action})

    def resync_project(self) -> dict:
        """重新启动项目: 把管理端新导入的邮箱补进项目待领池(只补新增)。"""
        return self._post("/api/projects/start", {"project_key": self.project_key})

    def refresh_status_map(self, ttl: float = 600.0) -> dict[str, tuple[str, str]]:
        """全量账号的 token 刷新状态映射: email -> (status, error)。缓存 10 分钟。

        兼容多种响应结构(refresh-status-list / external accounts)。
        """
        now = time.time()
        if now - getattr(self, "_rsm_ts", 0) < ttl and getattr(self, "_rsm_map", None):
            return self._rsm_map
        result: dict[str, tuple[str, str]] = {}
        for path in ("/api/accounts/refresh-status-list", "/api/external/accounts?limit=10000"):
            try:
                raw = self._get(path) or {}
            except Exception:
                continue
            buckets = []
            data = raw.get("data")
            if isinstance(data, dict):
                buckets = [data]
            elif isinstance(data, list):
                buckets = data
            for b in buckets if buckets else [raw]:
                if not isinstance(b, dict):
                    continue
                for lst_key in ("accounts", "items", "list", "rows"):
                    for a in (b.get(lst_key) or []):
                        if not isinstance(a, dict):
                            continue
                        em = str(a.get("email") or "").lower()
                        st = str(a.get("last_refresh_status") or a.get("refresh_status") or "")
                        er = str(a.get("last_refresh_error") or a.get("refresh_error") or "")
                        if em and st:
                            result[em] = (st, er)
                if result:
                    break
            if result:
                break
        self._rsm_ts = now
        self._rsm_map = result
        return result

    def get_account_credentials(self, email: str) -> dict | None:
        """按邮箱取账号四件套(email/password/client_id/refresh_token)。

        密码走二次验证接口(Web 登录密码)。找不到账号返回 None。
        """
        r = self._get("/api/accounts/search", {"q": email, "limit": 20})
        d = r.get("data") or {}
        accs = d.get("accounts") or d.get("items") or r.get("accounts") or []
        acc_id = None
        exact = str(email).lower()
        for a in accs if isinstance(accs, list) else []:
            if str(a.get("email") or "").lower() == exact:
                acc_id = a.get("id")
                break
        if not acc_id:
            return None
        detail = self._get(f"/api/accounts/{acc_id}")
        d = detail.get("data") if isinstance(detail.get("data"), dict) else {}
        a = detail.get("account") or d.get("account") or {}
        if not a:
            return None
        pw = ""
        try:
            rv = self._post("/api/export/verify", {"password": self.password})
            token = rv.get("verify_token") or (rv.get("data") or {}).get("verify_token")
            if token:
                r2 = self._sess.post(
                    f"{self.base_url}/api/accounts/export-selected",
                    json={"account_ids": [acc_id], "verify_token": token},
                    headers={"X-CSRFToken": self._csrf} if self._csrf else {},
                    timeout=60)
                for line in r2.text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "@" not in line:
                        continue
                    parts = line.split("----") if "----" in line else line.split("====")
                    parts = [x.strip() for x in parts]
                    if len(parts) >= 2 and parts[0].lower() == exact:
                        pw = parts[1]
                        break
        except Exception:
            pw = ""
        return {
            "email": str(a.get("email") or email),
            "password": pw,
            "client_id": str(a.get("client_id") or ""),
            "refresh_token": str(a.get("refresh_token") or ""),
        }

    def fetch_emails(self, email: str, top: int = 15) -> list[dict]:
        """读指定邮箱最新邮件(inbox+junk 合并), 返回统一结构的列表。"""
        data = self._get(f"/api/emails/{email}", {"folder": "all", "top": top}) or {}
        msgs = data.get("emails") or []
        out = []
        for m in msgs if isinstance(msgs, list) else []:
            if not isinstance(m, dict):
                continue
            out.append({
                "id": m.get("id"),
                "subject": m.get("subject") or "",
                "from": m.get("from") or "",
                "date": m.get("date") or "",
                "body": m.get("body_preview") or "",
                "_fetch_source": "manager",
            })
        return out


_client: ManagerClient | None = None
_client_lock = threading.Lock()


def get_client() -> ManagerClient:
    global _client
    from config import email as _email_cfg
    base = str(getattr(_email_cfg, "EMAIL_MANAGER_BASE_URL", "") or "").strip()
    if not base:
        raise RuntimeError("EMAIL_MANAGER_BASE_URL 未配置")
    with _client_lock:
        if _client is None or _client.base_url != base:
            _client = ManagerClient(
                base_url=base,
                password=str(getattr(_email_cfg, "EMAIL_MANAGER_PASSWORD", "") or ""),
                project_key=str(getattr(_email_cfg, "EMAIL_MANAGER_PROJECT_KEY", "gpt") or "gpt"),
                lease_seconds=int(getattr(_email_cfg, "EMAIL_MANAGER_LEASE_SECONDS", 900) or 900),
            )
        return _client


def manager_enabled() -> bool:
    from config import email as _email_cfg
    return bool(str(getattr(_email_cfg, "EMAIL_MANAGER_BASE_URL", "") or "").strip())
