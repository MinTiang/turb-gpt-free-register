# -*- coding: utf-8 -*-
"""Clash(mihomo) 节点控制:实验用出口轮换。

通过 external-controller API 切换「🚀 手动切换」组的选中节点,
并用代理出口 IP 验证切换生效。仅实验脚本使用,不接入注册主流程。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

CONTROLLER = "http://127.0.0.1:9097"
SECRET = "set-your-secret"
PROXY = "http://127.0.0.1:7897"
GROUP = "🚀 手动切换"
# 流量信息伪装节点,不可选
_FAKE = ("剩余流量", "套餐到期")


def _api(path: str, method: str = "GET", body: dict | None = None, timeout: float = 5.0):
    req = urllib.request.Request(
        f"{CONTROLLER}{path}",
        headers={"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"},
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def list_nodes() -> list[str]:
    """可切换的真实节点列表(排除流量/到期信息项)。"""
    enc = urllib.parse.quote(GROUP)
    p = _api(f"/proxies/{enc}")
    return [n for n in p.get("all", []) if not any(f in n for f in _FAKE)]


def current_node() -> str:
    enc = urllib.parse.quote(GROUP)
    return str(_api(f"/proxies/{enc}").get("now") or "")


def switch_node(name: str, *, verify_ip: bool = True, retries: int = 3) -> dict:
    """切换到指定节点并验证出口 IP。返回 {ok, node, ip, error?}。"""
    enc = urllib.parse.quote(GROUP)
    try:
        _api(f"/proxies/{enc}", method="PUT", body={"name": name})
    except Exception as exc:
        return {"ok": False, "node": name, "error": f"切换失败: {exc}"}
    # 切换后旧连接可能复用,等待新连接拿到新出口
    ip = ""
    if verify_ip:
        import requests as _r
        last_err = ""
        for attempt in range(retries):
            time.sleep(1.5 + attempt)
            try:
                resp = _r.get("https://api.ipify.org", proxies={"http": PROXY, "https": PROXY}, timeout=10)
                if resp.status_code == 200:
                    ip = resp.text.strip()
                    if ip:
                        break
            except Exception as exc:
                last_err = str(exc)[:120]
        if not ip:
            return {"ok": False, "node": name, "error": f"出口 IP 验证失败: {last_err}"}
    logger.info("[Clash] 已切换节点 %s, 出口 IP=%s", name, ip or "?")
    return {"ok": True, "node": name, "ip": ip}


def probe_node_ip(name: str) -> str:
    """临时切到某节点探测出口 IP 后切回原节点。返回 IP(失败空串)。"""
    before = current_node()
    result = switch_node(name)
    if before and before != name:
        try:
            enc = urllib.parse.quote(GROUP)
            _api(f"/proxies/{enc}", method="PUT", body={"name": before})
        except Exception:
            pass
    return result.get("ip") or ""
