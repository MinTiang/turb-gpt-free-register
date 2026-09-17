# -*- coding: utf-8 -*-
"""Patchright 本地浏览器注册配置（免费开源，无需任何 license/API Key）。

Patchright 是 Playwright 的反检测补丁分支：驱动侧不再泄漏
`navigator.webdriver`、`Runtime.enable` 等自动化特征。建议配合本机已安装的
正式 Chrome/Edge（PATCHRIGHT_CHANNEL）使用，浏览器指纹即真实浏览器指纹；
不要与 playwright-stealth 等注入式补丁叠加，否则会互相打架反而更容易被识别。
"""
from config.env_loader import apply_env_overrides

# 浏览器内核："chrome"=本机正式 Chrome（推荐），"msedge"=本机 Edge，
# "chromium"=patchright 自带的补丁版 Chromium（需先执行 patchright install chromium）。
# 留空则依次尝试 chrome → msedge → chromium。
PATCHRIGHT_CHANNEL: str = ""

# 显式指定浏览器可执行文件路径；非空时优先于 PATCHRIGHT_CHANNEL。
PATCHRIGHT_EXECUTABLE_PATH: str = ""

# 是否无头启动：False=显示窗口（过检更稳），True=无头（适合后台批量）。
PATCHRIGHT_HEADLESS: bool = False

# 使用当前出口 IP 自动匹配时区/语言。
PATCHRIGHT_GEOIP: bool = True

# 显式指定语言/时区；留空则在 PATCHRIGHT_GEOIP=True 时按出口 IP 自动推断。
# 例如：PATCHRIGHT_LOCALE="ja-JP"，PATCHRIGHT_TIMEZONE="Asia/Tokyo"。
PATCHRIGHT_LOCALE: str = ""
PATCHRIGHT_TIMEZONE: str = ""

# 是否把本项目传入/代理池抽取的代理传给浏览器。
PATCHRIGHT_USE_PROXY: bool = True

# 持久化用户目录；留空则临时上下文。批量注册建议留空，避免账号间共享缓存。
PATCHRIGHT_USER_DATA_DIR: str = ""

# 与 Roxy/Cloak Selenium 流程共用的超时时间。
PATCHRIGHT_SELENIUM_TIMEOUT: int = 90

# 调试时保留浏览器不自动关闭。
PATCHRIGHT_KEEP_BROWSER_OPEN: bool = False

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {'PATCHRIGHT_CHANNEL': 'str', 'PATCHRIGHT_EXECUTABLE_PATH': 'str', 'PATCHRIGHT_HEADLESS': 'bool', 'PATCHRIGHT_GEOIP': 'bool', 'PATCHRIGHT_LOCALE': 'str', 'PATCHRIGHT_TIMEZONE': 'str', 'PATCHRIGHT_USE_PROXY': 'bool', 'PATCHRIGHT_USER_DATA_DIR': 'str', 'PATCHRIGHT_SELENIUM_TIMEOUT': 'int', 'PATCHRIGHT_KEEP_BROWSER_OPEN': 'bool'})
