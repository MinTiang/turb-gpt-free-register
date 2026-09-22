# -*- coding: utf-8 -*-
"""通过 CloakBrowser + Playwright 适配层执行 ChatGPT 注册。"""
from __future__ import annotations

import json
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

        submit_round = 0
        while True:
            submit_round += 1
            try:
                _submit_email_and_wait_next(
                    driver,
                    email,
                    attempts=2,
                    email_supplier=_email_supplier_after_input,
                )
                break
            except Exception as exc:
                if submit_round >= 3:
                    raise
                logger.warning(
                    "[Cloak注册] 第 %s 次邮箱提交失败,回登录页恢复后重试: %s",
                    submit_round, str(exc)[:150],
                )
                try:
                    driver.get("https://chatgpt.com/auth/login")
                    human_delay("navigate")
                    _maybe_accept(driver)
                except Exception:
                    pass
        _check_manual_stop()

        # ==================== 页面状态机 ====================
        # 不再按固定线性步骤走:每轮观察当前页面,是什么页面就分发到对应
        # 处理器(验证码/密码页/重置密码/资料页/质询/登录态),任何意外页面
        # 只是本轮不匹配,轮询重看——卡死点从"流程错位"降级为"多等一轮"。
        # 页面语义:
        #   /reset-password            → 老账号(密码未知):完成重置即恢复注册
        #   log-in/password            → 老账号(密码已知):填库中密码直接登录
        #   email-verification         → 验证码:取码填入;新号+密码开关先切"使用密码继续"
        #   create-account/password    → 新号密码创建页:生成并写入密码
        #   about-you                  → 填姓名生日
        from core.page_ops import (
            _is_email_verification_page,
            _is_login_password_page,
            _has_access_token,
            _solve_cloudflare_challenge_if_present,
            _click_continue_with_password_if_present,
            _fill_password_page_if_present,
            _human_type_text,
        )

        stored_password = None
        try:
            from core import db as _db
            _acc = _db.get_account_by_email(email) if email else None
            if _acc:
                _extra = json.loads(str(_acc.get("extra_json") or "{}"))
                stored_password = str(_extra.get("registration_password") or "").strip() or None
        except Exception:
            stored_password = None
        if stored_password:
            logger.info("[Cloak注册][页面机] 该邮箱本地已有注册密码(老账号),将尝试密码登录/重置")

        password_enabled = bool(getattr(_register_cfg, "REGISTER_WITH_PASSWORD", True))

        def _has_visible_password_input() -> bool:
            try:
                return bool(driver.execute_script(
                    "const v=el=>!!(el&&(el.offsetWidth||el.offsetHeight||el.getClientRects().length));"
                    "return [...document.querySelectorAll('input')].some(el=>v(el)&&el.type==='password')"))
            except Exception:
                return False

        def _gen_password() -> str:
            import secrets as _sec
            alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
            special = "!@#$%^&*"
            body = "".join(_sec.choice(alphabet) for _ in range(10))
            tail = _sec.choice(special) + "".join(_sec.choice("0123456789") for _ in range(2))
            return body + tail

        def _click_submit_button() -> bool:
            for xp in ("//button[@type='submit']",
                       "//button[contains(., 'Continue')]", "//button[contains(., '继续')]"):
                try:
                    els = list(driver.find_elements("xpath", xp))
                except Exception:
                    continue
                for el in els:
                    try:
                        if el.is_displayed() and el.is_enabled():
                            el.click()
                            return True
                    except Exception:
                        continue
            return False

        deadline = time.time() + 900.0
        last_state_log = 0.0
        email_submit_count = 1
        switch_tried = False
        # ---- 每状态时间卡点: 从该状态首次进入起累计计时, 处理动作不重置预算 ----
        state_budgets = {
            "otp": 300.0,          # 验证码页总停留
            "reset": 360.0,        # 重置密码页总停留
            "create_pw": 150.0,    # 创建密码页
            "login_pw": 120.0,     # 登录密码页
            "profile": 120.0,      # about-you 资料页
            "auth_error": 90.0,    # 授权错误页恢复宽限
            "login_page": 180.0,   # 登录首页(邮箱提交不前进)
        }
        state_first: dict = {}
        login_logged = False
        otp_used = set()
        otp_attempts = 0
        reset_code_ts = None
        reset_code_filled = False
        reset_password = None
        last_url = ""
        while time.time() < deadline:
            _check_manual_stop()
            try:
                last_url = str(driver.current_url or "")
            except Exception:
                last_url = ""
            low = last_url.lower()

            now = time.time()
            if now - last_state_log >= 8.0:
                last_state_log = now
                left = deadline - now
                logger.info("[Cloak注册][页面机] 观察 url=%s (全局剩余 %.0fs)", last_url[:110], max(0.0, left))

            # 状态分类: 每个状态首次进入起累计计时(处理动作不重置预算)
            st = None
            if "/reset-password" in low:
                st = "reset"
            elif "/create-account/password" in low:
                st = "create_pw"
            elif "email-verification" in low:
                st = "otp"
            elif "/log-in" in low:
                st = "login_pw"
            elif "/about-you" in low:
                st = "profile"
            elif "/auth/error" in low:
                st = "auth_error"
            elif "chatgpt.com/auth/login" in low:
                st = "login_page"
            if st:
                first = state_first.setdefault(st, now)
                over = now - first - state_budgets.get(st, 300.0)
                if over > 0:
                    if st == "otp":
                        if not otp_used:
                            raise RuntimeError(
                                "验证码页停留超时且从未取到验证码,邮箱疑似死号,该邮箱作废")
                        raise RuntimeError(
                            "验证码页停留超时(取到码但持续无效),该邮箱作废")
                    if st == "reset":
                        if reset_code_filled:
                            raise RuntimeError("重置密码页停留超时(码已填但未完成),该邮箱作废")
                        raise RuntimeError(
                            "重置密码页停留超时且未收到重置码,邮箱疑似死号,该邮箱作废")
                    raise RuntimeError(f"页面状态[{st}]停留超时 {over:.0f}s,该邮箱作废")

            # Turnstile 质询:能点就点(短超时,不阻塞其它状态判断)
            try:
                if _solve_cloudflare_challenge_if_present(driver, timeout=3.0):
                    continue
            except Exception:
                pass

            # 授权错误页(error=undefined 等):回登录页重新走
            if "/auth/error" in low:
                logger.warning("[Cloak注册][页面机] 授权错误页,回登录页重来")
                try:
                    driver.get("https://chatgpt.com/auth/login")
                    human_delay("navigate")
                    _maybe_accept(driver)
                except Exception:
                    pass
                continue

            # 封号快速失败:提交凭据后 OpenAI 给 account_deactivated 页,重试无意义
            if "auth.openai.com" in low:
                try:
                    body_head = str(driver.execute_script(
                        "return (document.body.innerText||'').slice(0,3000)") or "").lower()
                except Exception:
                    body_head = ""
                if "account_deactivated" in body_head or "deleted or deactivated" in body_head:
                    raise RuntimeError(
                        "账号已被 OpenAI 停用/封禁(account_deactivated),该邮箱为废号,"
                        "请从邮箱池移除后换新邮箱重试"
                    )

            # 登录态已建立 → 注册/恢复完成
            try:
                if _has_access_token(driver):
                    if not login_logged:
                        login_logged = True
                        logger.info("[Cloak注册][页面机] 已检测到登录态(accessToken),流程完成")
                    if not profile_done:
                        create_acknowledged = True
                    break
            except Exception:
                pass

            # 重置密码页:老账号密码未知 → 取邮件码 + 设置新密码完成恢复
            if "/reset-password" in low:
                if reset_code_ts is None:
                    reset_code_ts = time.time()
                    otp_after_ts = reset_code_ts
                    reset_code_filled = False
                    logger.info("[Cloak注册][页面机] 重置密码页:老账号恢复流程,取码基准已重置")
                if not reset_code_filled:
                    try:
                        reset_otp = otp_code if (otp_code and not otp_used) else wait_for_otp(email, after_ts=reset_code_ts)
                    except Exception as exc:
                        logger.warning("[Cloak注册][页面机] 重置码未收到,继续等待: %s", str(exc)[:120])
                        time.sleep(1.0)
                        continue
                    otp_used.add(str(reset_otp))
                    reset_code_filled = True
                    logger.info("[Cloak注册][页面机] 重置码收到:%s,填入", reset_otp)
                    code_xps = ["//input[contains(@autocomplete,'one-time-code')]",
                                "//input[contains(@id,'code') or contains(@name,'code')]",
                                "//input[@inputmode='numeric' or @type='tel']"]
                    filled = False
                    for xp in code_xps:
                        try:
                            els = [e for e in driver.find_elements("xpath", xp) if e.is_displayed()]
                        except Exception:
                            continue
                        if els:
                            _human_type_text(driver, els[0], str(reset_otp))
                            filled = True
                            break
                    if not filled:
                        reset_code_filled = False
                    human_delay("otp_input")
                    continue
                if not reset_password:
                    reset_password = _gen_password()
                    logger.info("[Cloak注册][页面机] 填入新密码并提交")
                try:
                    pw_els = [e for e in driver.find_elements("css selector", "input[type='password']") if e.is_displayed()]
                except Exception:
                    pw_els = []
                for el in pw_els[:2]:
                    try:
                        _human_type_text(driver, el, reset_password)
                    except Exception as exc:
                        logger.warning("[Cloak注册][页面机] 新密码填入失败: %s", str(exc)[:100])
                _click_submit_button()
                human_delay("navigate")
                continue

            # 登录密码页:老账号(密码已知)直接登录
            # (/log-in 页也会内嵌密码框,不能只认 _is_login_password_page 的路径判定)
            if _is_login_password_page(driver) or ("/log-in" in low and _has_visible_password_input()):
                pwd = stored_password or (reset_password if reset_code_filled else None) or openai_password
                if pwd:
                    logger.info("[Cloak注册][页面机] 登录密码页:使用已知密码登录")
                    try:
                        pw_els = [e for e in driver.find_elements("css selector", "input[type='password']") if e.is_displayed()]
                        if pw_els:
                            _human_type_text(driver, pw_els[0], pwd)
                            _click_submit_button()
                            human_delay("navigate")
                    except Exception as exc:
                        logger.warning("[Cloak注册][页面机] 密码登录失败: %s", str(exc)[:120])
                    continue
                logger.info("[Cloak注册][页面机] 登录密码页:无已知密码,点击忘记密码走重置")
                forgot = False
                for xp in ("//a[contains(@href,'reset')]", "//button[contains(., 'Forgot')]",
                           "//a[contains(., 'Forgot')]", "//button[contains(., '忘记')]"):
                    try:
                        els = [e for e in driver.find_elements("xpath", xp) if e.is_displayed()]
                    except Exception:
                        continue
                    if els:
                        els[0].click()
                        forgot = True
                        break
                if not forgot:
                    _click_submit_button()
                human_delay("navigate")
                continue

            # 创建密码页(新号+密码开关):生成并写入密码
            if "/create-account/password" in low:
                got = _fill_password_page_if_present(driver, email, timeout=20)
                if got:
                    openai_password = got
                    otp_after_ts = time.time()
                    logger.info("[Cloak注册][页面机] 已创建账号密码,OTP 取码基准重置")
                human_delay("navigate")
                continue

            # 验证码页
            if _is_email_verification_page(driver):
                if password_enabled and not switch_tried and not stored_password and not openai_password:
                    switch_tried = True
                    switch = _click_continue_with_password_if_present(driver)
                    logger.info("[Cloak注册][页面机] 密码切换结果: %s", switch.get("reason"))
                    human_delay("form")
                    continue
                if otp_attempts >= 3:
                    raise RuntimeError("邮箱验证码连续错误/过期(账号疑似封禁或风控),已达最大重试次数,该邮箱作废")
                otp_attempts += 1
                logger.info("[Cloak注册][页面机][OTP] 等待验证码(第 %s/3 次)", otp_attempts)
                code = None
                if otp_code and not otp_used:
                    code = otp_code
                else:
                    try:
                        code = wait_for_otp(email, after_ts=otp_after_ts)
                    except Exception as exc:
                        if otp_attempts >= 3:
                            raise RuntimeError(
                                "多轮未收到验证码,邮箱疑似死号,该邮箱作废"
                            )
                        logger.warning("[Cloak注册][页面机][OTP] 未收到验证码,点重发后继续: %s", str(exc)[:150])
                        otp_after_ts = time.time()
                        _click_resend_email_otp(driver, timeout=25)
                        human_delay("api")
                        continue
                otp_used.add(str(code))
                logger.info("[Cloak注册][页面机][OTP] 收到验证码:%s", code)
                _clear_otp_inputs(driver)
                _type_otp(driver, code)
                human_delay("otp_input")
                try:
                    _click_continue(driver)
                except Exception:
                    pass
                outcome = _wait_after_email_otp_submit(driver, timeout=10)
                logger.info("[Cloak注册][页面机][OTP] 提交后状态:%s", outcome)
                if outcome != "accepted":
                    otp_after_ts = time.time()
                    resend = _click_resend_email_otp(driver, timeout=25)
                    if resend.get("reason") == "left_verification_page":
                        continue
                    human_delay("api")
                continue

            # 邮箱输入页再现(登录流重启):重填邮箱(最多再填 2 次)
            try:
                has_email_input = driver.execute_script(
                    "const v=el=>!!(el&&(el.offsetWidth||el.offsetHeight||el.getClientRects().length));"
                    "return [...document.querySelectorAll('input')].some(el=>v(el)&&"
                    "(el.type==='email'||el.name==='email'||el.id==='email'||el.autocomplete==='email'))")
            except Exception:
                has_email_input = False
            if has_email_input and email_submit_count < 3:
                email_submit_count += 1
                logger.info("[Cloak注册][页面机] 邮箱输入页再现,重填并提交(第 %s 次)", email_submit_count)
                _submit_email_and_wait_next(driver, email, attempts=1)
                human_delay("form")
                continue
            if has_email_input and email_submit_count >= 3:
                raise RuntimeError(
                    "邮箱多轮验证码无效且流程反复重启(账号状态异常),该邮箱作废"
                )

            # 资料页(about-you):仅在页面上出现时才进入(避免在其它页误等 5s+刷日志)
            if "about-you" in low:
                try:
                    if _complete_profile_page(driver, name, birthday, timeout=15):
                        profile_done = True
                        create_acknowledged = True
                        logger.info("[Cloak注册][页面机] 资料页已提交")
                        human_delay("post_auth")
                except Exception as exc:
                    logger.warning("[Cloak注册][页面机] 资料页处理异常(继续观察): %s", str(exc)[:120])
                continue

        if time.time() >= deadline and not _has_access_token(driver):
            raise RuntimeError(f"页面状态机超时(900s),最后页面: {last_url[:160]}")

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
                # 浏览器通道复用当前窗口跑 CLI 授权：CF 盾和注册登录态都在这个
                # 窗口里。clear_existing_state=False 保留登录态，避免重新走一遍
                # 邮箱验证。新账号缺 Codex 权益时授权流程会出手机验证页，由
                # 接码渠道完成（这是拿到能出量的 Codex 凭证的唯一途径）。
                from core.browser_codex_oauth import run_browser_codex_oauth
                logger.info("[Cloak注册][Codex] 复用当前 CloakBrowser 窗口执行 Codex 授权（保留登录态）")
                _check_manual_stop()
                codex_result = run_browser_codex_oauth(
                    email,
                    reuse_existing_profile=True,
                    existing_driver=driver,
                    existing_opened=opened,
                    force=True,
                    clear_existing_state=False,
                )
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
                _perm = any(k in str(exc) for k in ("停用/封禁", "重置密码页", "废号", "该邮箱作废", "邮箱疑似死号"))
                if "邮箱疑似死号" in str(exc):
                    _rel = "disabled"
                else:
                    _rel = "failed" if (create_acknowledged or _perm) else "available"
                if _perm:
                    logger.warning("[Cloak注册] 永久性失败,邮箱标记 failed 不再回池: %s", email)
                release_email(email, status=_rel, note=f"Cloak注册失败: {str(exc)[:180]}")
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
            quit_ok = True
            def _quit():
                nonlocal quit_ok
                try:
                    driver.quit()
                except Exception:
                    quit_ok = False
            _run_with_timeout(_quit, 30, label="浏览器关闭")
            # quit 挂死/异常时浏览器进程树还活着(每个 200-500MB,过夜泄漏
            # 可把容器内存吃满——实测 20G),必须按启动时记录的标记强杀兜底。
            if not quit_ok:
                try:
                    from core.cloakbrowser_driver import force_kill_browser
                    force_kill_browser(driver)
                except Exception as exc:
                    logger.warning("[Cloak注册] 强杀浏览器失败: %s: %s", type(exc).__name__, str(exc)[:120])



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
