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

        # 首次邮箱提交只做一次(带内置稳定检测); 失败即任务失败回池。
        # 其后的页面再现(登录流重启)统一由下方状态机的 email 状态处理,
        # 不再有第二套计数器。
        _submit_email_and_wait_next(
            driver,
            email,
            attempts=2,
            email_supplier=_email_supplier_after_input,
        )
        _check_manual_stop()

        # ==================== 页面状态机(识别 → 处理一次 → 等页面变化) ====================
        # 核心规则(修复"同一页面被反复匹配、重复操作"):
        #   1) 每轮探测一次 DOM 快照, 分类出唯一状态
        #   2) 新状态: 执行处理动作一次, 记录动作时间
        #   3) 同一状态: 只等待不操作; 超过宽限仍未变化 → 有限重试 → 仍不前进才失败
        #   4) 全局 300s 硬顶; 完成判定只用同步探测
        # 状态语义:
        #   banned      OpenAI 明示账号停用 → 失败(failed)
        #   home        到达 ChatGPT → 完成
        #   auth_error  /auth/error → 回登录页重来
        #   profile     about-you/姓名年龄 → 填一次
        #   code        验证码/重置码输入框 → 取码填一次(含密码分支切换)
        #   pw_new      创建/重置密码页 → 生成密码填一次
        #   pw_login    登录密码页 → 已知密码登录; 未知则点忘记密码
        #   email       邮箱输入页 → 填一次
        from core.page_ops import (
            _click_continue_with_password_if_present,
            _human_type_text,
            _has_access_token_sync,
            _solve_cloudflare_challenge_if_present,
        )

        _PROBE_JS = (
            "const vis=el=>!!(el&&(el.offsetWidth||el.offsetHeight||el.getClientRects().length));"
            "const ins=[...document.querySelectorAll('input')].filter(vis);"
            "const body=(document.body&&document.body.innerText||'').slice(0,2500).toLowerCase();"
            "return {"
            " url: location.href,"
            " email: ins.some(el=>el.type==='email'||el.autocomplete==='email'||el.name==='email'||el.id==='email'),"
            " pw: ins.some(el=>el.type==='password'),"
            " code: ins.some(el=>String(el.autocomplete||'').includes('one-time-code')||el.inputmode==='numeric'),"
            " profile: ins.some(el=>el.autocomplete==='name'||el.name==='name')&&ins.some(el=>el.name==='age'),"
            " turnstile: !!document.querySelector('input[id^=\'cf-chl-widget\']'),"
            " banned: body.includes('account_deactivated')||body.includes('deleted or deactivated')"
            "};"
        )

        def _probe() -> dict:
            try:
                r = driver.execute_script(_PROBE_JS) or {}
            except Exception:
                r = {}
            return {
                "url": str(r.get("url") or ""),
                "email": bool(r.get("email")),
                "pw": bool(r.get("pw")),
                "code": bool(r.get("code")),
                "profile": bool(r.get("profile")),
                "turnstile": bool(r.get("turnstile")),
                "banned": bool(r.get("banned")),
            }

        def _classify(p: dict) -> str:
            low = p["url"].lower()
            if p["banned"]:
                return "banned"
            if "chatgpt.com" in low and "/auth/" not in low:
                return "home"
            if "/auth/error" in low:
                return "auth_error"
            if "/about-you" in low or p["profile"]:
                return "profile"
            if p["code"] and not p["email"] and not p["pw"]:
                return "code"
            if p["pw"]:
                if "/create-account/password" in low or "/reset-password" in low:
                    return "pw_new"
                return "pw_login"
            if p["email"]:
                return "email"
            return "unknown"

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

        deadline = time.time() + 300.0
        last_log = 0.0
        last_state = None          # 上一个执行过动作的状态
        state_ts = 0.0             # 该状态最近一次动作时间
        state_tries: dict = {}     # 每状态已执行动作次数
        max_tries = {"email": 3, "code": 3, "pw_new": 2, "pw_login": 2,
                     "profile": 2, "auth_error": 2}
        wait_nav = 30.0            # 动作后等页面变化的宽限
        switch_tried = False
        profile_done = False
        reset_password = None
        otp_used = set()
        last_url = ""

        while time.time() < deadline:
            _check_manual_stop()
            p = _probe()
            st = _classify(p)
            last_url = p["url"]
            now = time.time()
            if now - last_log >= 8.0:
                last_log = now
                logger.info("[Cloak注册][页面机] 状态=%s url=%s (剩余 %.0fs)",
                            st, last_url[:100], max(0.0, deadline - now))

            if st == "banned":
                raise RuntimeError("账号已被 OpenAI 停用/封禁(account_deactivated),不可注册")
            if st == "home":
                logger.info("[Cloak注册][页面机] 已到达 ChatGPT, 注册/恢复完成")
                create_acknowledged = True
                break

            # Turnstile 质询独立处理: 检测到就点, 不占用状态动作次数
            if p["turnstile"]:
                try:
                    if _solve_cloudflare_challenge_if_present(driver, timeout=5.0):
                        continue
                except Exception:
                    pass

            if st == "unknown":
                time.sleep(0.8)
                continue

            if st == last_state:
                # 同一页面: 只等待, 不重复操作
                if time.time() - state_ts < wait_nav:
                    time.sleep(0.8)
                    continue
                if state_tries.get(st, 0) >= max_tries.get(st, 2):
                    raise RuntimeError(
                        f"页面[{st}]处理 {state_tries.get(st, 0)} 次后仍未前进,任务失败(邮箱回池,由连续失败机制退役)")
            else:
                last_state = st
                state_tries[st] = 0
                if st == "code":
                    otp_after_ts = time.time()

            state_tries[st] = state_tries.get(st, 0) + 1
            state_ts = time.time()

            if st == "auth_error":
                logger.warning("[Cloak注册][页面机] 授权错误页,回登录页重来(第 %s 次)", state_tries[st])
                try:
                    driver.get("https://chatgpt.com/auth/login")
                    human_delay("navigate")
                    _maybe_accept(driver)
                except Exception:
                    pass
                continue

            if st == "email":
                logger.info("[Cloak注册][页面机] 邮箱页:填写并提交(第 %s 次)", state_tries[st])
                try:
                    _submit_email_and_wait_next(driver, email, attempts=1)
                except Exception as exc:
                    logger.warning("[Cloak注册][页面机] 邮箱提交异常: %s", str(exc)[:140])
                human_delay("form")
                continue

            if st == "code":
                # 新号+密码开关: 首次进验证码页先切"使用密码继续"
                if (password_enabled and not switch_tried and not stored_password
                        and not openai_password and "email-verification" in p["url"]):
                    switch_tried = True
                    sw = _click_continue_with_password_if_present(driver)
                    logger.info("[Cloak注册][页面机] 密码切换结果: %s", sw.get("reason"))
                    human_delay("form")
                    continue
                tries = state_tries[st]
                if tries > 1:
                    otp_after_ts = time.time()
                    _click_resend_email_otp(driver, timeout=15)
                logger.info("[Cloak注册][页面机] 验证码页:取码填入(第 %s 次)", tries)
                code = None
                if otp_code and not otp_used:
                    code = otp_code
                else:
                    try:
                        code = wait_for_otp(email, after_ts=otp_after_ts)
                    except Exception as exc:
                        # 取码失败: 缩短同页宽限到 5s 后即可重试
                        state_ts = time.time() - (wait_nav - 5.0)
                        if tries >= 3:
                            raise RuntimeError("多轮未收到验证码,邮箱疑似死号")
                        logger.warning("[Cloak注册][页面机] 未收到验证码: %s", str(exc)[:120])
                        continue
                otp_used.add(str(code))
                logger.info("[Cloak注册][页面机] 收到验证码: %s", code)
                _clear_otp_inputs(driver)
                _type_otp(driver, code)
                human_delay("otp_input")
                try:
                    _click_continue(driver)
                except Exception:
                    pass
                _wait_after_email_otp_submit(driver, timeout=10)
                continue

            if st == "pw_new":
                if not reset_password:
                    reset_password = _gen_password()
                if not openai_password:
                    openai_password = reset_password
                logger.info("[Cloak注册][页面机] 密码页:生成并填入新密码(第 %s 次)", state_tries[st])
                try:
                    pw_els = [e for e in driver.find_elements("css selector", "input[type='password']") if e.is_displayed()]
                except Exception:
                    pw_els = []
                for el in pw_els[:2]:
                    try:
                        _human_type_text(driver, el, openai_password)
                    except Exception as exc:
                        logger.warning("[Cloak注册][页面机] 密码填入失败: %s", str(exc)[:100])
                _click_submit_button()
                human_delay("form")
                continue

            if st == "pw_login":
                pwd = stored_password or reset_password or openai_password
                if pwd:
                    logger.info("[Cloak注册][页面机] 登录密码页:使用已知密码登录(第 %s 次)", state_tries[st])
                    try:
                        pw_els = [e for e in driver.find_elements("css selector", "input[type='password']") if e.is_displayed()]
                        if pw_els:
                            _human_type_text(driver, pw_els[0], pwd)
                            _click_submit_button()
                            human_delay("navigate")
                    except Exception as exc:
                        logger.warning("[Cloak注册][页面机] 密码登录失败: %s", str(exc)[:120])
                    continue
                logger.info("[Cloak注册][页面机] 登录密码页:无已知密码,点忘记密码走重置")
                forgot = False
                for xp in ("//a[contains(@href,'reset')]", "//button[contains(., 'Forgot')]",
                           "//a[contains(., 'Forgot')]", "//button[contains(., '忘记')]",
                           "//a[contains(., '忘记')]"):
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

            if st == "profile":
                logger.info("[Cloak注册][页面机] 资料页:填写并提交(第 %s 次)", state_tries[st])
                try:
                    if _complete_profile_page(driver, name, birthday, timeout=15):
                        profile_done = True
                        create_acknowledged = True
                except Exception as exc:
                    logger.warning("[Cloak注册][页面机] 资料页处理异常: %s", str(exc)[:120])
                human_delay("post_auth")
                continue

            time.sleep(0.8)
        else:
            if not _has_access_token_sync(driver):
                raise RuntimeError(f"注册超时(300s),最后状态: {last_state} url={last_url[:140]}")

        session_info = _fetch_chatgpt_session(driver, timeout=30)
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
                # 死邮箱(停用): 多轮完全收不到邮件, 邮箱本身不可用
                if "邮箱疑似死号" in str(exc):
                    _rel = "disabled"
                    logger.warning("[Cloak注册] 邮箱疑似死号,标记停用: %s", email)
                # 流程失败但邮箱是活的(OTP 无效/重置未完成/账号异常):
                # 标 failed, 不再自动回池空转, 但可在邮箱库手动改回 available 重试
                elif any(k in str(exc) for k in ("停用/封禁", "重置密码页", "废号", "该邮箱作废")):
                    _rel = "failed"
                    logger.warning("[Cloak注册] 流程失败(邮箱可用),标记 failed 可手动重试: %s", email)
                else:
                    _rel = "failed" if create_acknowledged else "available"
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
