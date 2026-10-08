# -*- coding: utf-8 -*-
"""手动注册抓包工具: 起 CloakBrowser(带 HAR 录制), 你手动完成注册,
关闭后自动解析 HAR 提取协议参数(build_id/client_version/sentinel版本),
并输出一份 human 流程时间线供协议链路对齐。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HAR_PATH = Path(__file__).resolve().parent.parent / "logs" / "manual_capture.har"
HAR_PATH.parent.mkdir(parents=True, exist_ok=True)
HAR_PATH.unlink(missing_ok=True)


def main() -> None:
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    log = logging.getLogger("capture")

    from playwright.sync_api import sync_playwright

    print("=" * 56)
    print("手动注册抓包模式")
    print("1. 浏览器马上打开 chatgpt.com/auth/login")
    print("2. 你手动完成一次注册(建议用一个干净新邮箱)")
    print("3. 到达 ChatGPT 首页后, 回到这个窗口按回车结束")
    print("=" * 56)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            proxy={"server": "http://127.0.0.1:7897"} if _clash_alive() else None,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            record_har_path=str(HAR_PATH),
            record_har_mode="minimal",
            locale="en-US",
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        page.goto("https://chatgpt.com/auth/login", wait_until="commit", timeout=60000)
        log.info("浏览器已就绪, 等你完成注册...")

        # 轮询等待你完成: 检测 accessToken 出现(登录成功标志)
        got = False
        t0 = time.time()
        while time.time() - t0 < 1800:
            try:
                token = page.evaluate(
                    "fetch('/api/auth/session',{credentials:'include'})"
                    ".then(r=>r.json()).then(j=>!!(j&&j.accessToken)).catch(()=>false)")
            except Exception:
                token = False
            if token:
                log.info("检测到登录成功(accessToken 已出现)")
                got = True
                break
            time.sleep(5)
        if not got:
            log.warning("30 分钟未检测到登录, 按已有流量继续解析")
        try:
            ctx.close()   # 关闭 context 落盘 HAR
        except Exception:
            pass
        browser.close()

    parse_har(HAR_PATH)


def _clash_alive() -> bool:
    import socket
    s = socket.socket(); s.settimeout(1)
    try:
        s.connect(("127.0.0.1", 7897)); return True
    except Exception:
        return False
    finally:
        s.close()


def parse_har(har_path: Path) -> None:
    print("\n" + "=" * 56)
    print("HAR 解析结果")
    print("=" * 56)
    try:
        har = json.loads(har_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"HAR 读取失败: {exc}")
        return
    entries = har.get("log", {}).get("entries", [])
    print(f"共 {len(entries)} 条请求")

    build_id = client_ver = client_build = sentinel_ver = None
    timeline = []
    for e in entries:
        url = e.get("request", {}).get("url", "")
        hdrs = {h["name"].lower(): h["value"] for h in e.get("request", {}).get("headers", [])}
        resp_hdrs = {h["name"].lower(): h["value"] for h in e.get("response", {}).get("headers", [])}
        if not client_ver and hdrs.get("oai-client-version"):
            client_ver = hdrs["oai-client-version"]
        if not client_build and hdrs.get("oai-client-build-number"):
            client_build = hdrs["oai-client-build-number"]
        if "sentinel" in url:
            import re
            m = re.search(r"/sentinel/([^/]+)/", url)
            if m and not sentinel_ver:
                sentinel_ver = m.group(1)
        # 主文档里的 build hash(从响应内容抓, minimal 模式可能没有 body, 用 _next/static 资产名兜底)
        import re
        if not build_id:
            m = re.search(r"(prod-[a-f0-9]{40})", url)
            if m:
                build_id = m.group(1)
        # 关键节点时间线
        for key, tag in (
            ("/auth/login", "登录页"),
            ("/api/accounts/authorize", "authorize"),
            ("/email-otp/send", "发码"),
            ("/email-otp/validate", "验码"),
            ("/create-account/password", "设密码"),
            ("/about-you", "资料页"),
            ("/api/auth/callback", "回调"),
            ("/api/auth/session", "session"),
        ):
            if key in url:
                timeline.append((e.get("startedDateTime", "")[11:19], tag, e.get("response", {}).get("status")))

    print("\n--- 协议参数 ---")
    print(f"OPENAI_BUILD_ID       = {build_id or '(未捕获, 需从主文档响应找)'}")
    print(f"OAI_CLIENT_VERSION    = {client_ver or '(未捕获)'}")
    print(f"OAI_CLIENT_BUILD_NUMBER = {client_build or '(未捕获)'}")
    print(f"SENTINEL_SV           = {sentinel_ver or '(未捕获)'}")
    print("\n--- 注册流程时间线(供节奏对齐) ---")
    seen = set()
    for t, tag, st in timeline:
        k = (tag, st)
        if k in seen:
            continue
        seen.add(k)
        print(f"  {t}  {tag}  -> {st}")
    print(f"\nHAR 已保存: {har_path}")


if __name__ == "__main__":
    main()
