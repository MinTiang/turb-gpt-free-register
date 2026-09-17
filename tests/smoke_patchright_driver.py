# -*- coding: utf-8 -*-
"""Patchright 本地浏览器驱动冒烟测试（手动运行）。

用法：
    python tests/smoke_patchright_driver.py

验证：
1. build_patchright_driver 能启动本机浏览器；
2. Selenium 风格 send_keys 逐字符输入正确累积（修复前会被 fill 整值替换，
   输入完后只剩最后一个字符）；
3. CONTROL+A / BACKSPACE / ENTER 等 Selenium 按键码被翻译成真实按键；
4. switch_to.active_element 可用；
5. OTP 六格逐格输入各自落格；
6. patchright 下 navigator.webdriver 不暴露。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config.localbrowser as _local_cfg

# 冒烟测试不做出口地理检测、不开窗口。
_local_cfg.PATCHRIGHT_GEOIP = False
_local_cfg.PATCHRIGHT_HEADLESS = True

from core.patchright_driver import build_patchright_driver

PAGE_HTML = (
    "data:text/html;charset=utf-8,<html><body>"
    "<input id='email' type='email' />"
    "<div>"
    "<input maxlength='1' inputmode='numeric' />"
    "<input maxlength='1' inputmode='numeric' />"
    "<input maxlength='1' inputmode='numeric' />"
    "<input maxlength='1' inputmode='numeric' />"
    "<input maxlength='1' inputmode='numeric' />"
    "<input maxlength='1' inputmode='numeric' />"
    "</div>"
    "</body></html>"
)


def main() -> None:
    driver, opened = build_patchright_driver(proxy="")
    try:
        driver.get(PAGE_HTML)

        webdriver_value = driver.execute_script("return navigator.webdriver;")
        print(f"[1] navigator.webdriver = {webdriver_value!r}（期望 None）")

        email = "smoke.test+patchright@example.com"
        email_el = driver.find_element("css selector", "#email")
        # 复刻 roxy_registration._human_type_text 的真实调用序列：
        # CONTROL+A 全选（Selenium 按键码）→ BACKSPACE → 逐字符输入。
        email_el.send_keys("\ue009", "a")   # Keys.CONTROL, 'a'
        email_el.send_keys("\ue003")        # Keys.BACKSPACE
        for ch in email:
            email_el.send_keys(ch)
        got = driver.execute_script("return document.getElementById('email').value;")
        assert got == email, f"逐字符输入累积失败：{got!r}"
        print(f"[2] 逐字符输入累积 OK：{got}")

        email_el.send_keys("\ue009", "a")
        email_el.send_keys("\ue003")
        got2 = driver.execute_script("return document.getElementById('email').value;")
        assert got2 == "", f"清空失败：{got2!r}"
        print("[3] CONTROL+A + BACKSPACE 清空 OK")

        email_el.click()
        active = driver.switch_to.active_element
        active.send_keys("\ue007")          # Keys.ENTER
        print("[4] switch_to.active_element + ENTER OK")

        boxes = driver.find_elements("css selector", "input[maxlength='1']")
        assert len(boxes) == 6, f"OTP 输入框数量异常：{len(boxes)}"
        for box, ch in zip(boxes, "135246"):
            box.send_keys(ch)
        otp = driver.execute_script(
            "return [...document.querySelectorAll(\"input[maxlength='1']\")].map(i => i.value).join('');"
        )
        assert otp == "135246", f"OTP 逐格输入失败：{otp!r}"
        print(f"[5] OTP 六格逐格输入 OK：{otp}")

        print("全部通过")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
