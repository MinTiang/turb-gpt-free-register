# -*- coding: utf-8 -*-
"""通过 CloakBrowser + Playwright 适配层执行 ChatGPT 注册。"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from config import cloakbrowser as _cfg
from config import twofa as _twofa_cfg
from config import register as _register_cfg
from core.account_export import save_account_data, post_register_dwell
from core.browser_data_saver import BrowserDataSaver
from core.browser_traffic import PlaywrightTrafficTracker
from core.cloakbrowser_driver import build_cloak_driver
from core.email_provider import acquire_email_after_input, wait_for_otp, resolve_email_source
from core.humanize import delay as human_delay

# 复用共享页面操作函数库(core/page_ops.py,原 roxy_registration 提取)。
from core.page_ops import (  # noqa: F401
    _maybe_accept, _submit_email_and_wait_next, _fill_password_page_if_present,
    _clear_otp_inputs, _type_otp, _click_continue, _wait_after_email_otp_submit,
    _click_resend_email_otp, _complete_profile_page, _fetch_chatgpt_session, _check_manual_stop,
)

logger = logging.getLogger(__name__)


def _export_browser_cookies(driver) -> list:
    """导出 CloakBrowser 当前全部 cookie，供协议层复用登录态。

    platform 免接码授权在协议层跑，拿不到 Selenium 会话；把浏览器已建立的
    cookie 注入新建 BrowserSession，authorize 才能像复用注册 session 那样直接放行。

    浏览器卡态时 get_cookies 可能挂住，因此走看门狗；失败返回空列表
    （调用方降级为全新登录，不中断注册主流程）。
    """
    try:
        raw = _run_with_timeout(lambda: driver.get_cookies(), 15.0, label="导出cookie")
    except Exception as exc:
        logger.warning("[Cloak注册] 导出 cookie 失败: %s: %s", type(exc).__name__, str(exc)[:120])
        return []
    if not raw:
        logger.warning("[Cloak注册] 未导出到 cookie，platform 授权将按全新登录走")
        return []
    # 只保留 openai 相关域，避免把无关 cookie 带进协议会话
    picked = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        domain = str(c.get("domain") or "").lower()
        if any(d in domain for d in ("chatgpt.com", "openai.com")):
            picked.append(c)
    logger.info("[Cloak注册] 已导出 %d 条 openai 域 cookie（共 %d 条）", len(picked), len(raw))
    return picked


def _run_with_timeout(fn, seconds: float, *, label: str):
    """带看门狗超时地执行收尾调用;超时放弃(线程留守)并返回 None。

    Cloak 的流量统计/监听器移除在浏览器页面卡态时可能被 CDP 调用无限挂住,
    曾导致 worker 单线程卡死不进下一任务。注册主流程的成果(token)必须先行,
    收尾统计属于可弃部分。
    """
    result: dict = {}

    def _target():
        try:
            result["value"] = fn()
        except Exception as exc:
            result["error"] = exc

    worker = threading.Thread(target=_target, daemon=True, name=f"timeout-{label}")
    worker.start()
    worker.join(max(1.0, float(seconds)))
    if worker.is_alive():
        logger.warning("[Cloak注册] %s 超时 %.0fs,放弃收尾统计并继续(线程留守)", label, seconds)
        return None
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _run_cloak_registration_impl(
    email: str | None,
    name: str,
    birthday: str,
    proxy: str = None,
    otp_code: str = None,
    batch_dir: Path | None = None,
    on_email_acquired: Callable[[str], None] | None = None,
) -> dict:
    """CloakBrowser 自动化注册入口。"""
    driver = None
    opened = None
    create_acknowledged = False
    openai_password: str | None = None
    traffic_tracker: PlaywrightTrafficTracker | None = None
    data_saver: BrowserDataSaver | None = None
    network_traffic: dict | None = None
    try:
        driver, opened = build_cloak_driver(proxy=proxy)
        try:
            traffic_tracker = PlaywrightTrafficTracker(driver.context, label="Cloak")
        except Exception as exc:
            # 统计失败不应影响注册主流程。
            logger.warning("[Cloak注册] 初始化浏览器流量统计失败，继续注册：%s: %s", type(exc).__name__, str(exc)[:180])
        data_saver = BrowserDataSaver(label="Cloak")
        if traffic_tracker is not None:
            traffic_tracker.attach_data_saver(data_saver)
        data_saver.install_playwright(driver.context)
        logger.info("[Cloak注册] 开始：%s，profile=%s", email, opened.profile_id)

        otp_after_ts = time.time()
        logger.info("[Cloak注册] 打开登录页：https://chatgpt.com/auth/login")
        driver.get("https://chatgpt.com/auth/login")
        human_delay("navigate")
        _maybe_accept(driver)
        _check_manual_stop()

        def _email_supplier_after_input() -> str:
            nonlocal email
            _check_manual_stop()
            email = acquire_email_after_input(email)
            if on_email_acquired:
                on_email_acquired(email)
            return email

        next_state = _submit_email_and_wait_next(
            driver,
            email,
            attempts=3,
            email_supplier=_email_supplier_after_input,
        )
        _check_manual_stop()

        # 密码分支(REGISTER_WITH_PASSWORD 配置驱动):验证码页点击“使用密码继续”
        # 切换到密码表单,设置密码后回到验证码页继续 OTP 流程。
        if bool(getattr(_register_cfg, "REGISTER_WITH_PASSWORD", True)):
            from core.page_ops import _click_continue_with_password_if_present
            switch = _click_continue_with_password_if_present(driver)
            logger.info("[Cloak注册] 密码切换结果: %s", switch.get("reason"))
            human_delay("form")
        # _fill_password_page_if_present 会在设置成功后返回本次 OpenAI 注册密码。
        openai_password = _fill_password_page_if_present(driver, email, timeout=25)
        _check_manual_stop()
        if openai_password:
            # 密码设置(user/register)会触发一次新的 email-otp/send;重置取码基准,
            # 避免取到切换密码前验证码页发出的旧验证码。
            otp_after_ts = time.time()

        current_otp = otp_code
        max_otp_attempts = 3
        for otp_attempt in range(1, max_otp_attempts + 1):
            if current_otp is None:
                logger.info("[Cloak注册][OTP] 等待验证码：%s（第 %s/%s 次）", email, otp_attempt, max_otp_attempts)
                try:
                    current_otp = wait_for_otp(email, after_ts=otp_after_ts)
                except Exception as exc:
                    if otp_attempt >= max_otp_attempts:
                        raise
                    logger.warning(
                        "[Cloak注册][OTP] 一直未收到验证码，点击“重新发送电子邮件”后继续等待（下一轮 %s/%s）：%s: %s",
                        otp_attempt + 1,
                        max_otp_attempts,
                        type(exc).__name__,
                        str(exc)[:180],
                    )
                    otp_after_ts = time.time()
                    _click_resend_email_otp(driver, timeout=25)
                    human_delay("api")
                    current_otp = None
                    continue
            logger.info("[Cloak注册][OTP] 收到验证码：%s", current_otp)
            _clear_otp_inputs(driver)
            _type_otp(driver, current_otp)
            human_delay("otp_input")
            try:
                _click_continue(driver)
            except Exception as exc:
                logger.info("[Cloak注册][OTP] 未找到显式提交按钮，继续等待页面状态：%s", str(exc)[:120])

            outcome = _wait_after_email_otp_submit(driver, timeout=10)
            if outcome == "accepted":
                break
            if otp_attempt >= max_otp_attempts:
                raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
            otp_after_ts = time.time()
            resend = _click_resend_email_otp(driver, timeout=25)
            if resend.get("reason") == "left_verification_page":
                logger.info("[Cloak注册][OTP] 重发时发现页面已跳转,按验证通过继续")
                break
            human_delay("api")
            current_otp = None

        profile_submitted = _complete_profile_page(driver, name, birthday, timeout=60)
        if profile_submitted:
            create_acknowledged = True
            human_delay("post_auth")

        session_info = _fetch_chatgpt_session(driver, timeout=120)
        access_token = session_info["accessToken"]
        logger.info("[Cloak注册] 已拿到 accessToken：%s", email)

        if _twofa_cfg.ENABLE_2FA:
            logger.warning("[Cloak注册] 当前 CloakBrowser 自动化路径暂不执行 2FA 设置，已跳过")
        totp_secret = None

        codex_result = {
            "status": "skipped",
            "ok": True,
            "message": "ENABLE_CODEX_AUTO=False，跳过 Codex",
        }
        try:
            from config import codex as _codex_cfg
            if bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False)):
                oauth_driver = str(getattr(_codex_cfg, "CODEX_OAUTH_DRIVER", "") or "").strip().lower()
                # 浏览器通道一律走浏览器版 platform 免接码授权：CF 盾、登录态、
                # Turnstile 都在这个窗口里；授权地址用 platform client（app_2SKx…，
                # 免手机号）。之前走协议接口（platform 协议版 / CPA 的 CLI client
                # 浏览器版）都被 CF 或手机验证页挡住——实测 2026-09-22。
                # clear_existing_state 概念已不适用：新流程不清理任何状态。
                from core.codex_platform_oauth import run_platform_codex_oauth_browser
                logger.info(
                    "[Cloak注册][Codex] 浏览器内走 platform 免接码授权"
                    "（client=%s，保留注册登录态）",
                    "app_2SKx(platform)",
                )
                _check_manual_stop()
                codex_result = run_platform_codex_oauth_browser(driver, email, proxy=proxy)
            else:
                logger.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=False，注册后跳过 Codex OAuth")
        except Exception as exc:
            codex_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {str(exc)[:180]}"}

        # 统计注册浏览器关闭前的完整会话；注册后停留期间的网络请求也计入。
        post_register_dwell(email, label="Cloak注册")
        logger.info("[Cloak注册] 停留结束,开始收尾(流量统计→入库)")
        if traffic_tracker is not None:
            network_traffic = _run_with_timeout(traffic_tracker.stop, 90, label="流量统计收尾") or network_traffic
        if data_saver is not None:
            _run_with_timeout(data_saver.stop, 20, label="省流量收尾")
        logger.info("[Cloak注册] 收尾完成,入库账号")
        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=resolve_email_source(email),
            proxy_used=((opened.raw or {}).get("proxy") if opened else None) or proxy or None,
            batch_dir=batch_dir,
            registration_channel="browse",
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "cloakbrowser": {"profile_id": opened.profile_id, "open_result": opened.raw},
                "registration_password": openai_password,
                "codex": codex_result,
                "network_traffic": network_traffic,
            },
        )
        codex_ok = codex_result.get("ok") or codex_result.get("status") == "skipped"
        return {
            "success": bool(codex_ok),
            "email": email,
            "account_id": account_id,
            "access_token": access_token,
            "totp_secret": totp_secret,
            "codex": codex_result,
            "network_traffic": network_traffic,
            "error": None if codex_ok else f"Codex 未完成: {codex_result.get('message')}",
        }
    except Exception as exc:
        if traffic_tracker is not None:
            try:
                network_traffic = _run_with_timeout(traffic_tracker.stop, 60, label="流量统计收尾(异常路径)") or network_traffic
            except Exception:
                pass
        if data_saver is not None:
            _run_with_timeout(data_saver.stop, 15, label="省流量收尾(异常路径)")
        logger.error("[Cloak注册] 失败：%s: %s", type(exc).__name__, exc)
        logger.debug("[Cloak注册] 失败详情", exc_info=True)
        try:
            if email:
                from core.email_provider import release_email
                release_email(email, status="failed" if create_acknowledged else "available", note=f"Cloak注册失败: {str(exc)[:180]}")
        except Exception:
            pass
        return {
            "success": False,
            "email": email,
            "network_traffic": network_traffic,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
    finally:
        if traffic_tracker is not None:
            _run_with_timeout(traffic_tracker.stop, 30, label="流量统计收尾(finally)")
        if data_saver is not None:
            _run_with_timeout(data_saver.stop, 10, label="省流量收尾(finally)")
        if driver and not bool(_cfg.CLOAK_KEEP_BROWSER_OPEN):
            _run_with_timeout(driver.quit, 30, label="浏览器关闭")



def run_cloak_registration(
    email: str | None,
    name: str,
    birthday: str,
    proxy: str = None,
    otp_code: str = None,
    batch_dir: Path | None = None,
    on_email_acquired: Callable[[str], None] | None = None,
) -> dict:
    """线程隔离入口。

    cloakbrowser 内部使用 Playwright Sync API,其事件循环绑定在启动线程上:
    同一线程第二次启动必报 "Sync API inside asyncio loop"。因此每个注册任务
    在专属短命线程中执行,循环状态随线程销毁,WebUI 复用线程不再受影响。
    """
    result_box: dict = {}

    def _target():
        result_box["result"] = _run_cloak_registration_impl(
            email=email,
            name=name,
            birthday=birthday,
            proxy=proxy,
            otp_code=otp_code,
            batch_dir=batch_dir,
            on_email_acquired=on_email_acquired,
        )

    # 线程名从父线程派生:任务的 FileHandler 过滤器据此把过程日志写入本任务文件。
    worker = threading.Thread(
        target=_target,
        name=f"{threading.current_thread().name}+cloak",
        daemon=True,
    )
    worker.start()
    worker.join(900)
    if worker.is_alive():
        logger.error("[Cloak注册] 任务超时(900s),放弃本次(浏览器线程留守,稍后自动退出)")
        return {"success": False, "email": email, "error": "Cloak 注册超时(900s)"}
    result = result_box.get("result")
    if result is None:
        return {"success": False, "email": email, "error": "Cloak 注册线程异常退出"}
    return result
