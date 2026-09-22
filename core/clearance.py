# -*- coding: utf-8 -*-
"""Cloudflare clearance 刷新（FlareSolverr）。

auth.openai.com / chatgpt.com 对注册与授权链路的请求做概率性 CF 拦截
（实测 2026-09-22: 免费节点池通过率约 50%，被拦表现为 403 "Just a moment"
质询页，继续重试会升级为 429 rate_limit）。

FlareSolverr 用无头浏览器真实过一遍质询，产出 cf_clearance + __cf_bm
等 cookie 与匹配的 User-Agent。把这对 (cookie, UA) 注入后续请求即可
显著降低被拦概率（grok2api 生产做法）。

配置（config/cloakbrowser.py 之外的独立段，见 config/clearance.py）：
    CLEARANCE_MODE          = "none" | "flaresolverr"
    CLEARANCE_FLARESOLVERR_URL = "http://127.0.0.1:8191"
    CLEARANCE_TIMEOUT_SEC   = 60
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Cloudflare 系 cookie 名（注入成效判定用；与 core/session.py 的同类常量语义一致）
_CF_COOKIE_NAMES = (
    "cf_clearance", "__cf_bm", "__cfseq", "__cflb", "_cfuvid",
    "cf_chl_rc_i", "cf_chl_rc_ni", "cf_chl_rc_m",
)


@dataclass
class ClearanceBundle:
    """一次 clearance 刷新产物。"""

    host: str = ""
    cookies: dict = field(default_factory=dict)
    user_agent: str = ""
    fetched_at: float = 0.0

    def is_valid(self, ttl_seconds: float = 900) -> bool:
        return bool(self.cookies) and (time.time() - self.fetched_at) < ttl_seconds

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


# 按 host 缓存(同 host 复用,避免频繁解质询)
_CACHE: dict[str, ClearanceBundle] = {}
_LOCK = threading.Lock()


def _cfg():
    from config import clearance as cfg
    return cfg


def _is_cf_challenge(resp) -> bool:
    """判断响应是否为 Cloudflare 质询页（可被 FlareSolverr 解开）。

    注意区分两类失败（2026-09-22 实测结论）：
      - CF 质询页（403 "Just a moment" / challenge-platform）：**可解**，
        刷新 cf_clearance 后重试有机会放行。
      - 429 rate_limit_exceeded（JSON 错误体）：说明请求已通过 CF 到达应用层，
        是 OpenAI 侧对"未登录态 + 该邮箱"的短时频率限制，**clearance 无效**，
        只能退避等待或换账号。
    """
    try:
        status = int(getattr(resp, "status_code", 0) or 0)
    except Exception:
        return False
    if status not in (403, 503):
        return False
    text = str(getattr(resp, "text", "") or "")[:3000]
    markers = (
        "Just a moment",
        "challenge-platform",
        "Checking your browser",
        "Attention Required",
    )
    if any(m in text for m in markers):
        return True
    try:
        headers = {str(k).lower(): str(v) for k, v in (getattr(resp, "headers", {}) or {}).items()}
    except Exception:
        headers = {}
    return "challenge" in headers.get("cf-mitigated", "").lower()


def is_rate_limited(resp) -> bool:
    """响应是否为限流（429 或 rate_limit_exceeded 错误体）。"""
    try:
        status = int(getattr(resp, "status_code", 0) or 0)
    except Exception:
        return False
    if status == 429:
        return True
    text = str(getattr(resp, "text", "") or "")[:800]
    return "rate_limit_exceeded" in text or "Too many requests" in text


def refresh_clearance(
    target_url: str,
    *,
    proxy_url: str = "",
    force: bool = False,
    timeout_sec: int | None = None,
) -> ClearanceBundle | None:
    """用 FlareSolverr 解 target_url 的 CF 质询并返回 clearance。

    proxy_url 传入时应为 FlareSolverr 容器可达的代理地址（容器内访问宿主机
    用 host.docker.internal）。失败返回 None（调用方降级为原行为）。

    注意（2026-09-22 实测）：cf_clearance 只在**会触发 CF 质询的页面 URL** 上产出。
    `https://auth.openai.com/log-in` 有，而 `https://auth.openai.com/` 与
    `/api/accounts/authorize?...` 这类 API 路径没有（它们走 __cf_bm 就放行，
    但被拦时无法自解）。因此这里统一用 host 的 /log-in 页面作为解质询入口，
    cf_clearance 是域级 cookie，对同域 API 请求同样有效。
    """
    cfg = _cfg()
    if str(getattr(cfg, "CLEARANCE_MODE", "none") or "none").strip().lower() != "flaresolverr":
        return None
    base = str(getattr(cfg, "CLEARANCE_FLARESOLVERR_URL", "") or "").strip().rstrip("/")
    if not base:
        logger.warning("[Clearance] 未配置 CLEARANCE_FLARESOLVERR_URL，跳过")
        return None
    parsed = urlparse(target_url)
    host = (parsed.hostname or "").lower()
    if not host:
        return None

    with _LOCK:
        cached = _CACHE.get(host)
        if cached and cached.is_valid() and not force:
            return cached

    # 选择解质询入口：优先同 host 的 /log-in（实测能产出 cf_clearance），
    # 否则退回原 URL。
    solve_url = target_url
    if "/log-in" not in urlparse(target_url).path:
        solve_url = f"{parsed.scheme or 'https'}://{host}/log-in"

    timeout = int(timeout_sec or getattr(cfg, "CLEARANCE_TIMEOUT_SEC", 60) or 60)
    payload: dict = {"cmd": "request.get", "url": solve_url, "maxTimeout": timeout * 1000}
    if proxy_url:
        payload["proxy"] = {"url": proxy_url}

    logger.info("[Clearance] 开始刷新 %s 的 CF clearance（入口 %s，FlareSolverr）...", host, solve_url[:80])
    t0 = time.time()
    try:
        req = urllib.request.Request(
            f"{base}/v1",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout + 30) as resp:
            result = json.loads(resp.read().decode())
    except Exception as exc:
        logger.warning("[Clearance] FlareSolverr 请求失败: %s: %s", type(exc).__name__, str(exc)[:160])
        return None

    if str(result.get("status") or "") != "ok":
        logger.warning("[Clearance] FlareSolverr 返回非 ok: %s", str(result.get("message"))[:200])
        return None

    solution = result.get("solution") or {}
    raw_cookies = solution.get("cookies") or []
    if not raw_cookies:
        logger.warning("[Clearance] FlareSolverr 未返回 cookie")
        return None

    # 只保留属于该域(或其父域)的 cookie
    picked: dict = {}
    for c in raw_cookies:
        name = str(c.get("name") or "").strip()
        domain = str(c.get("domain") or "").lstrip(".").lower()
        if not name:
            continue
        if domain and not (host == domain or host.endswith("." + domain)):
            continue
        picked[name] = c.get("value")
    if not picked:
        logger.warning("[Clearance] FlareSolverr cookie 域名过滤后为空")
        return None

    bundle = ClearanceBundle(
        host=host,
        cookies=picked,
        user_agent=str(solution.get("userAgent") or ""),
        fetched_at=time.time(),
    )
    with _LOCK:
        _CACHE[host] = bundle
    logger.info(
        "[Clearance] %s clearance 刷新完成 (%.1fs, %d cookies: %s) cf_clearance=%s",
        host, time.time() - t0, len(picked), ", ".join(sorted(picked.keys())),
        "有" if "cf_clearance" in picked else "无",
    )
    return bundle


def apply_clearance_to_session(session, bundle: ClearanceBundle | None) -> bool:
    """把 clearance 的全部 cookie 注入 curl_cffi 会话。

    实测（2026-09-22）关键结论：提升通过率的是 FlareSolverr 返回的**整套**
    cookie（`__cf_bm` / `__cfseq` / `oai-did` 等），而不仅是 `cf_clearance`。
    对照实验：带 FS cookie 5/5 通过 vs 无 cookie 2/5（同一节点同一 IP）。
    因此这里注入全部域名匹配的 cookie，并同步 UA（cookie 与 UA 需配套）。

    返回是否至少注入了一个 Cloudflare 系 cookie（表示可用于重试）。
    """
    if not bundle or not bundle.cookies:
        return False
    injected_cf = False
    for name, value in bundle.cookies.items():
        if value is None:
            continue
        for domain in (f".{bundle.host}", bundle.host):
            try:
                session.cookies.set(name, value, domain=domain, path="/")
            except Exception:
                pass
        if name in _CF_COOKIE_NAMES or name == "cf_clearance":
            injected_cf = True
    if bundle.user_agent:
        try:
            session.headers["User-Agent"] = bundle.user_agent
        except Exception:
            pass
        try:
            session.browser_profile["user_agent"] = bundle.user_agent
        except Exception:
            pass
    return injected_cf


def prime_session(session, host: str, *, proxy_url: str = "") -> bool:
    """会话预热：主动获取并注入 clearance cookie（不等被拦）。

    实测（2026-09-22）：免费节点池无 cookie 时通过率约 40%，注入 FlareSolverr
    的 cookie 后 5/5 通过。因此在注册/授权开始前主动预热收益明显。

    返回是否成功注入（mode=none / 失败时 False，调用方照常继续）。
    """
    try:
        bundle = refresh_clearance(f"https://{host}/", proxy_url=proxy_url)
        if not bundle:
            return False
        ok = apply_clearance_to_session(session, bundle)
        if ok:
            logger.info("[Clearance] 已为 %s 预热并注入 %d 个 cookie", host, len(bundle.cookies))
        return ok
    except Exception as exc:
        logger.warning("[Clearance] 预热异常(忽略): %s: %s", type(exc).__name__, str(exc)[:140])
        return False


def clear_cache(host: str | None = None) -> None:
    with _LOCK:
        if host:
            _CACHE.pop(host.lower(), None)
        else:
            _CACHE.clear()
