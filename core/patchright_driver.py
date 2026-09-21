# -*- coding: utf-8 -*-
"""Patchright 本地浏览器启动器。

Patchright（pip install patchright）是 Playwright 的反检测补丁分支，浏览器
在本机运行、完全免费。这里复用 Cloak 的 Selenium 风格适配层（含真实键盘
事件输入），注册流程与 Roxy/Cloak 共享同一套页面操作函数。

安装：
    pip install patchright
    patchright install chromium   # 仅 PATCHRIGHT_CHANNEL=chromium 时需要；
                                  # 默认优先使用本机已安装的 Chrome/Edge。
"""
from __future__ import annotations

import glob
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from config import localbrowser as _cfg

# CloakSeleniumDriver / 代理与出口地理工具只依赖 Playwright 语义和
# config.browser，与 CloakBrowser 本体无关，直接复用。
from core.cloakbrowser_driver import (  # noqa: F401
    CloakSeleniumDriver,
    CloakOpenResult,
    _normalize_proxy,
    _detect_cloak_exit_geo,
)

logger = logging.getLogger(__name__)


@dataclass
class PatchrightOpenResult:
    profile_id: str = "patchright"
    raw: dict | None = None


def _default_browsers_dir() -> str:
    """playwright 系浏览器的默认安装目录(镜像里走 PLAYWRIGHT_BROWSERS_PATH 卷)。"""
    if sys.platform.startswith("win"):
        return os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Caches/ms-playwright")
    return os.path.expanduser("~/.cache/ms-playwright")


def _ensure_chromium() -> None:
    """Chromium 二进制缺失时按需下载(容器部署:浏览器不进镜像、启动不阻塞,
    首次真实使用时才拉取到 PLAYWRIGHT_BROWSERS_PATH 卷,之后常驻复用)。

    只在 channel 走自带 chromium(空配置或 chromium 系)时检查;
    PATCHRIGHT_CHANNEL=chrome/msedge 用系统浏览器,无需下载。
    """
    channel = str(getattr(_cfg, "PATCHRIGHT_CHANNEL", "") or "").strip().lower()
    if channel in ("chrome", "msedge", "chrome-beta", "msedge-beta"):
        return
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or _default_browsers_dir()
    installed = any(
        os.path.exists(os.path.join(d, "INSTALLATION_COMPLETE"))
        for d in glob.glob(os.path.join(base, "chromium-*"))
    )
    if installed:
        return
    logger.warning("[Patchright] 未检测到 Chromium 二进制，开始按需下载（python -m patchright install chromium，首次约 150MB）...")
    subprocess.run(
        [sys.executable, "-m", "patchright", "install", "chromium"],
        check=True,
    )
    logger.info("[Patchright] Chromium 按需下载完成")


def _build_patchright_locale_options(proxy_url: str | None = None) -> dict:
    """生成语言/时区配置：显式配置优先，否则按出口 IP 自动推断。"""
    explicit_locale = str(getattr(_cfg, "PATCHRIGHT_LOCALE", "") or "").strip()
    explicit_timezone = str(getattr(_cfg, "PATCHRIGHT_TIMEZONE", "") or "").strip()
    out = {}
    if explicit_locale:
        out["locale"] = explicit_locale
        out["accept_language"] = f"{explicit_locale},{explicit_locale.split('-')[0]};q=0.9,en-US;q=0.8,en;q=0.7"
    if explicit_timezone:
        out["timezone"] = explicit_timezone
    if explicit_locale and explicit_timezone:
        return out
    if not bool(getattr(_cfg, "PATCHRIGHT_GEOIP", True)):
        return out
    try:
        from config.browser import build_browser_environment
        geo = _detect_cloak_exit_geo(proxy_url)
        profile = build_browser_environment(geo)
        out.setdefault("locale", str(profile.get("navigator_language") or ""))
        out.setdefault("timezone", str(profile.get("timezone_iana") or ""))
        out.setdefault("accept_language", str(profile.get("accept_language") or ""))
        out["geo"] = geo
    except Exception as exc:
        logger.debug("[Patchright] 构建自动语言/时区失败：%s: %s", type(exc).__name__, exc)
    return {k: v for k, v in out.items() if v}


def build_patchright_driver(proxy: str | None = None) -> tuple[CloakSeleniumDriver, PatchrightOpenResult]:
    """启动本地 Patchright 浏览器并返回 Selenium 风格 driver。

    proxy=None  时按 config.proxy.PROXY_POOL 随机抽取；
    proxy=""    时显式禁用代理；
    proxy="..." 时使用指定代理。
    """
    if proxy is None and bool(getattr(_cfg, "PATCHRIGHT_USE_PROXY", True)):
        try:
            from config.proxy import pick_proxy
            proxy = pick_proxy()
        except Exception:
            proxy = None
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "未安装 patchright，请执行：pip install patchright && patchright install chromium"
        ) from exc

    proxy_url = _normalize_proxy(proxy) if bool(getattr(_cfg, "PATCHRIGHT_USE_PROXY", True)) else None
    locale_opts = _build_patchright_locale_options(proxy_url)

    launch_kwargs: dict[str, Any] = {
        "headless": bool(getattr(_cfg, "PATCHRIGHT_HEADLESS", False)),
    }
    if proxy_url:
        launch_kwargs["proxy"] = {"server": proxy_url}
    executable = str(getattr(_cfg, "PATCHRIGHT_EXECUTABLE_PATH", "") or "").strip()
    channel = str(getattr(_cfg, "PATCHRIGHT_CHANNEL", "") or "").strip()
    if executable:
        launch_attempts = [{**launch_kwargs, "executable_path": executable}]
    elif channel:
        launch_attempts = [{**launch_kwargs, "channel": channel}]
    else:
        # 留空自动探测：优先本机正式 Chrome/Edge（指纹即真实浏览器），最后回退
        # patchright 自带的补丁版 Chromium（需 patchright install chromium）。
        launch_attempts = [
            {**launch_kwargs, "channel": "chrome"},
            {**launch_kwargs, "channel": "msedge"},
            dict(launch_kwargs),
        ]

    pw = sync_playwright().start()
    browser = None
    last_exc: Exception | None = None
    for attempt in launch_attempts:
        # 自带补丁版 Chromium 兜底分支:二进制缺失时按需下载(容器首用场景,
        # 落 PLAYWRIGHT_BROWSERS_PATH 卷;桌面端系统有 Chrome/Edge 则走不到这)
        if "executable_path" not in attempt and "channel" not in attempt:
            _ensure_chromium()
        try:
            browser = pw.chromium.launch(**attempt)
            launch_kwargs = attempt
            break
        except Exception as exc:
            last_exc = exc
            logger.debug("[Patchright] launch 尝试失败 %s：%s: %s", attempt.get("channel") or attempt.get("executable_path") or "chromium", type(exc).__name__, str(exc)[:160])
    if browser is None:
        pw.stop()
        raise RuntimeError(f"Patchright 浏览器启动失败（已尝试 {len(launch_attempts)} 种内核配置）: {last_exc}")

    context_kwargs: dict[str, Any] = {
        # 不锁定 viewport，使用真实窗口尺寸，避免自动化常见的固定视口指纹。
        "viewport": None,
    }
    if locale_opts.get("locale"):
        context_kwargs["locale"] = locale_opts["locale"]
    if locale_opts.get("timezone"):
        context_kwargs["timezone_id"] = locale_opts["timezone"]
    if locale_opts.get("accept_language"):
        context_kwargs["extra_http_headers"] = {"Accept-Language": locale_opts["accept_language"]}

    user_data_dir = str(getattr(_cfg, "PATCHRIGHT_USER_DATA_DIR", "") or "").strip()
    if user_data_dir:
        context = pw.chromium.launch_persistent_context(user_data_dir, **{**launch_kwargs, **context_kwargs})
        page = context.pages[0] if context.pages else context.new_page()
        browser = getattr(context, "browser", None) or context
    else:
        context = browser.new_context(**context_kwargs)
        page = context.new_page()

    driver = CloakSeleniumDriver(browser=browser, context=context, page=page)
    driver._registration_log_prefix = "[Patchright注册]"
    driver.set_page_load_timeout(int(getattr(_cfg, "PATCHRIGHT_SELENIUM_TIMEOUT", 90) or 90))
    logger.info(
        "[Patchright] 启动本地浏览器：headless=%s channel=%s proxy=%s locale=%s timezone=%s persistent=%s",
        launch_kwargs.get("headless"),
        launch_kwargs.get("channel") or (f"executable:{launch_kwargs['executable_path']}" if launch_kwargs.get("executable_path") else "chromium"),
        proxy_url or "无", locale_opts.get("locale") or "自动/默认",
        locale_opts.get("timezone") or "自动/默认", bool(user_data_dir),
    )
    return driver, PatchrightOpenResult(raw={
        "driver": "patchright",
        "proxy": proxy_url,
        "locale": locale_opts,
        "options": {"headless": launch_kwargs.get("headless"), "channel": launch_kwargs.get("channel", "chromium")},
    })
