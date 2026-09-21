# -*- coding: utf-8 -*-
"""Cloak 有头浏览器自主注册(带全量操作记录)。

用法:
    python tools/cloak_manual_register.py [--password]

记录内容:
    - 每一步操作与耗时(步骤日志)
    - 全部网络请求/响应(状态码)
    - 关键节点页面截图(注册日志/cloak-shots/)
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import logging

LOG_DIR = PROJECT_ROOT / "注册日志" / "cloak-shots"
LOG_DIR.mkdir(parents=True, exist_ok=True)
RUN_LOG = LOG_DIR / f"run-{int(time.time())}.log"
NET_LOG = LOG_DIR / f"net-{int(time.time())}.jsonl"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(RUN_LOG, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("cloak-manual")


def step(msg: str) -> None:
    logger.info("[步骤] %s (累计 %.1fs)", msg, time.time() - _T0)


_T0 = time.time()


def main() -> int:
    from core.outlook_client import pick_account
    from core.cloakbrowser_driver import build_cloak_driver
    from core.email_provider import wait_for_otp
    import core.roxy_registration as R
    from core.humanize import delay as human_delay

    want_password = "--password" in sys.argv

    EXPERIMENT_EMAIL = "homfwkl09081@outlook.com"  # 裸号存活实验专用,禁止注册
    while True:
        acct = pick_account()
        if acct.email != EXPERIMENT_EMAIL:
            break
        from core.outlook_client import release_account
        release_account(acct.email, status="available", note="裸号存活实验专用,跳过")
        logger.warning("跳过实验专用邮箱 %s", acct.email)
    email = acct.email
    step(f"实验邮箱: {email}")

    driver, opened = build_cloak_driver(proxy=None)
    logger.info("[Cloak] 浏览器已启动 headless=%s", opened.raw.get("options", {}).get("headless"))

    # 全量网络录制
    net_fh = open(NET_LOG, "a", encoding="utf-8")

    def on_response(resp):
        try:
            net_fh.write(json.dumps({
                "ts": time.time(),
                "status": resp.status,
                "method": resp.request.method,
                "url": resp.url[:300],
            }, ensure_ascii=False) + "\n")
            net_fh.flush()
        except Exception:
            pass

    try:
        driver.context.on("response", on_response)
    except Exception as exc:
        logger.warning("网络录制挂载失败: %s", exc)

    def shot(name: str) -> None:
        try:
            path = LOG_DIR / f"{int(time.time())}-{name}.png"
            driver.page.screenshot(path=str(path))
            step(f"截图: {path.name}")
        except Exception as exc:
            logger.warning("截图失败 %s: %s", name, exc)

    def current_url() -> str:
        try:
            return str(driver.page.url or "")
        except Exception:
            return ""

    try:
        # 步骤1: 首页顶层导航(比直打 /auth/login 更接近真实用户;带耗时)
        step("打开 chatgpt.com 首页")
        t = time.time()
        driver.get("https://chatgpt.com/")
        step(f"首页加载完成 {time.time() - t:.1f}s url={current_url()[:100]}")
        shot("1-home")
        human_delay("navigate")

        # 步骤2: 打开登录页
        step("打开 /auth/login")
        t = time.time()
        driver.get("https://chatgpt.com/auth/login")
        step(f"登录页加载完成 {time.time() - t:.1f}s url={current_url()[:100]}")
        shot("2-login")
        human_delay("navigate")

        R._maybe_accept(driver)
        step("cookie 弹窗处理完成")

        # 步骤3: 提交邮箱(进入密码页或验证码页)
        step("提交邮箱(等待下一页)")
        otp_after = time.time()
        state = R._submit_email_and_wait_next(driver, email, attempts=3)
        step(f"邮箱提交完成, 进入: {state} url={current_url()[:100]}")
        shot("3-after-email")

        # 步骤4: 密码分支(可选)
        password = None
        if want_password:
            step("尝试切换/填写密码表单")
            password = R._fill_password_page_if_present(driver, email, timeout=25)
            step(f"密码页结果: {'已设置密码' if password else '未出现密码页'}")
            shot("4-password")

        # 步骤5: OTP(验证码已在落地 email-verification 时发送)
        otp = wait_for_otp(email, after_ts=otp_after)
        step(f"收到 OTP: {otp}")

        # 步骤5.5: 密码分支 —— 结构定位点击 "使用密码继续" 并设置密码
        password = None
        if want_password:
            step("等待 CF 门控通过后再点击密码切换")
            R._solve_cloudflare_challenge_if_present(driver, timeout=180)
            step("结构定位点击 使用密码继续/Continue with password")
            try:
                switch = R._click_continue_with_password_if_present(driver)
                step(f"切换结果: {switch.get('reason')}")
                time.sleep(2)
                password = R._fill_password_page_if_present(driver, email, timeout=25)
                step(f"密码设置: {'成功' if password else '未出现密码页,继续 OTP 流程'}")
                shot("5b-password")
            except Exception as exc:
                step(f"密码分支失败(继续 OTP 流程): {str(exc)[:150]}")

        # 步骤6: 输入验证码 → 资料页(Turnstile 通过后 session 可能重发验证码,最多两轮)
        for verify_round in range(2):
            step(f"[第{verify_round + 1}轮] 输入验证码 {otp}")
            R._clear_otp_inputs(driver)
            R._type_otp(driver, otp)
            human_delay("otp_input")
            shot(f"5-otp-filled-r{verify_round}")
            step("点击 Continue 提交验证码")
            try:
                R._click_continue(driver)
            except Exception as exc:
                logger.info("无显式提交按钮: %s", str(exc)[:100])
            outcome = R._wait_after_email_otp_submit(driver, timeout=15)
            step(f"验证码提交结果: {outcome} url={current_url()[:100]}")
            shot(f"6-after-otp-r{verify_round}")

            if outcome == "accepted":
                step("验证码已接受,等待资料页(about-you)...")
            else:
                step(f"验证码结果异常({outcome}),等待页面变化...")

            # 步骤6.5: 资料页
            step("填写资料页(昵称/生日), 下一页: setting password / session")
            submitted = R._complete_profile_page(driver, "David Miller", "1995-06-15", timeout=60)
            step(f"资料页提交: {submitted} url={current_url()[:100]}")
            if submitted:
                shot(f"7-profile-r{verify_round}")
                break

            # 仍在验证码页 = Turnstile 通过后 session 重发了新验证码:再取一次
            if "email-verification" in current_url():
                step("检测到二次邮箱验证(Turnstile 重载后重发), 等待新验证码...")
                otp_after = time.time()
                try:
                    otp = wait_for_otp(email, after_ts=otp_after)
                    step(f"收到新 OTP: {otp}")
                    shot(f"6b-reverify-{verify_round}")
                    continue
                except Exception as exc:
                    step(f"二次验证码等待失败: {str(exc)[:150]}")
            break

        # 步骤7: session/AT
        step("拉取 ChatGPT session")
        session_info = R._fetch_chatgpt_session(driver, timeout=120)
        access_token = session_info["accessToken"]
        step(f"注册成功! accessToken={access_token[:24]}... user={session_info.get('user', {}).get('id')}")

        from core.account_export import save_account_data
        from core.email_provider import resolve_email_source
        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=None,
            email_source=resolve_email_source(email),
            proxy_used=opened.raw.get("proxy"),
            extra={
                "registration_password": password,
                "cloakbrowser": {"profile_id": opened.profile_id, "open_result": opened.raw},
                "user": session_info.get("user"),
                "account": session_info.get("account"),
            },
        )
        step(f"已入库: account_id={account_id}")
        shot("8-done")
        net_fh.close()
        return 0
    except Exception as exc:
        logger.error("[失败] %s: %s", type(exc).__name__, str(exc)[:300])
        logger.debug(traceback.format_exc())
        try:
            shot("err-last")
            step(f"失败时页面 url={current_url()[:150]}")
        except Exception:
            pass
        net_fh.close()
        # 失败时保留浏览器供人工检查
        logger.info("浏览器保留打开状态供人工检查; 关闭请直接关窗口")
        return 1


if __name__ == "__main__":
    sys.exit(main())
