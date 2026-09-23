# -*- coding: utf-8 -*-
"""CloakBrowser 的 Selenium 风格轻量适配层。"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import re
import time
from dataclasses import dataclass
from typing import Any

from config import cloakbrowser as _cfg

logger = logging.getLogger(__name__)

# Selenium Keys 私有区按键码 → Playwright 键名；send_keys 收到这些字符时
# 必须按真实按键处理，绝不能当作文本写入输入框。
_SELENIUM_KEY_NAMES = {
    "\ue003": "Backspace", "\ue004": "Tab", "\ue006": "Enter", "\ue007": "Enter",
    "\ue008": "Shift", "\ue009": "Control", "\ue00a": "Alt", "\ue00b": "Pause",
    "\ue00c": "Escape", "\ue00d": "Space", "\ue00e": "PageUp", "\ue00f": "PageDown",
    "\ue010": "End", "\ue011": "Home", "\ue012": "ArrowLeft", "\ue013": "ArrowUp",
    "\ue014": "ArrowRight", "\ue015": "ArrowDown", "\ue016": "Insert", "\ue017": "Delete",
    "\ue03d": "Meta",
}
_SELENIUM_FKEY_FIRST, _SELENIUM_FKEY_LAST = "\ue031", "\ue03c"
_SELENIUM_MODIFIERS = {"\ue008", "\ue009", "\ue00a", "\ue03d"}


def _selenium_key_name(ch: str) -> str | None:
    if ch in _SELENIUM_KEY_NAMES:
        return _SELENIUM_KEY_NAMES[ch]
    if _SELENIUM_FKEY_FIRST <= ch <= _SELENIUM_FKEY_LAST:
        return f"F{ord(ch) - ord(_SELENIUM_FKEY_FIRST) + 1}"
    return None


def _select_all_shortcut() -> str:
    # Selenium 的 CONTROL/COMMAND 全选要映射成宿主系统认识的真实组合键；
    # 固定 Meta+A 在 Windows/Linux 宿主上等于 Win 键+A，不会选中任何文本。
    try:
        import platform
        if platform.system().lower() == "darwin":
            return "Meta+A"
    except Exception:
        pass
    return "Control+A"


@dataclass
class CloakOpenResult:
    profile_id: str = "cloakbrowser"
    raw: dict | None = None


class CloakElement:
    def __init__(self, page, locator=None, handle=None):
        self.page = page
        self.locator = locator
        self.handle = handle

    def _handle(self):
        if self.handle is not None:
            return self.handle
        return self.locator.element_handle(timeout=5000)

    @property
    def text(self) -> str:
        """元素可见文本(page_ops 多处按 selenium 习惯访问 el.text)。"""
        try:
            return str(self._eval("el => el.innerText || el.textContent || ''") or "")
        except Exception:
            return ""

    def get_attribute(self, name: str):
        try:
            return self._eval("(el,a)=>el.getAttribute(a)", name)
        except Exception:
            return None

    def _eval(self, expression: str, arg: Any = None) -> Any:
        if self.locator is not None:
            try:
                return self.locator.evaluate(expression, arg, timeout=3000)
            except TypeError:
                return self.locator.evaluate(expression, arg)
        return self.handle.evaluate(expression, arg)

    def _eval_handle(self, expression: str, arg: Any = None) -> Any:
        h = self._handle()
        return h.evaluate_handle(expression, arg)

    def is_displayed(self) -> bool:
        try:
            if self.locator is not None:
                # 2026-09-18:cloak 的 stealth 环境下 locator.is_visible() 对可见
                # 元素返回 False(用户实测可见可输入但接口判不可见),导致按可见性
                # 过滤的代码全部失效。改用 JS 几何+样式判定。
                handle = None
                try:
                    handle = self._handle()
                except Exception:
                    handle = None
                if handle is not None:
                    try:
                        result = handle.evaluate(
                            "el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length) "
                            "&& getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none')"
                        )
                        return bool(result)
                    except Exception:
                        pass
                return bool(self.locator.is_visible(timeout=800))
            return bool(self.handle.evaluate("el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none')"))
        except Exception:
            return False

    def is_enabled(self) -> bool:
        try:
            if self.locator is not None:
                handle = None
                try:
                    handle = self._handle()
                except Exception:
                    handle = None
                if handle is not None:
                    try:
                        result = handle.evaluate(
                            "el => !!el && !el.disabled && el.getAttribute('aria-disabled') !== 'true'"
                        )
                        return bool(result)
                    except Exception:
                        pass
                return bool(self.locator.is_enabled(timeout=800))
            return bool(self.handle.evaluate("el => !el.disabled && el.getAttribute('aria-disabled') !== 'true'"))
        except Exception:
            return False

    def click(self) -> None:
        if self.locator is not None:
            self.locator.click(timeout=10000)
        else:
            self.handle.click(timeout=10000)

    def clear(self) -> None:
        try:
            if self.locator is not None:
                self.locator.fill("", timeout=10000)
            else:
                self.handle.fill("", timeout=10000)
        except Exception:
            # 部分非 input 元素不支持 fill，回退键盘清空。
            self.click()
            self.page.keyboard.press(_select_all_shortcut())
            self.page.keyboard.press("Backspace")

    def _focus_for_typing(self) -> None:
        # Selenium 的 send_keys 不点击元素；这里只在目标未持焦点的场合补一次
        # 点击，避免逐字符输入时每次 send_keys 都重复点击。
        try:
            focused = bool(self._eval(
                "el => !!el && (el === document.activeElement || el.contains(document.activeElement))"
            ))
        except Exception:
            focused = False
        if focused:
            return
        try:
            self.click()
        except Exception:
            try:
                if self.locator is not None:
                    self.locator.focus(timeout=3000)
                else:
                    self.handle.focus()
            except Exception:
                pass

    def _send_key_chord(self, text: str) -> None:
        """把 Selenium 按键序列（含私有区按键码）翻译成真实键盘事件。

        Selenium 语义里 CONTROL/COMMAND 后跟字符是组合键；项目内实际只用
        到 `全选`（mod+a），统一映射成宿主可识别的 Ctrl+A / Meta+A。
        """
        self._focus_for_typing()
        keyboard = self.page.keyboard
        buffer = ""
        pending_modifier = ""
        for ch in text:
            key_name = _selenium_key_name(ch)
            if key_name is not None and ch in _SELENIUM_MODIFIERS:
                if buffer:
                    keyboard.type(buffer, delay=0)
                    buffer = ""
                pending_modifier = key_name
                continue
            if pending_modifier:
                # 修饰键后的第一个字符按组合键处理（项目内主要是全选 mod+a）。
                if buffer:
                    keyboard.type(buffer, delay=0)
                    buffer = ""
                if ch.lower() == "a":
                    keyboard.press(_select_all_shortcut())
                else:
                    keyboard.press(f"{pending_modifier}+{(key_name or ch).upper()}")
                pending_modifier = ""
                continue
            if key_name is None:
                buffer += ch
                continue
            if buffer:
                keyboard.type(buffer, delay=0)
                buffer = ""
            keyboard.press(key_name)
        if buffer:
            try:
                keyboard.type(buffer, delay=0)
            except Exception:
                pass

    def send_keys(self, *values: str) -> None:
        text = "".join(str(v or "") for v in values)
        if not text:
            return
        if any("\ue000" <= ch <= "\ue0ff" for ch in text):
            # 含 Selenium 按键码：走真实键盘事件，禁止 fill（fill 会把按键码
            # 当文本写入，且整值替换会吞掉已输入内容）。
            self._send_key_chord(text)
            return
        self._focus_for_typing()
        try:
            # 逐字符真实键盘事件：追加在光标处，触发 keydown/keypress/input，
            # 语义与 Selenium send_keys 一致；人工节奏由上层 humanize 控制。
            self.page.keyboard.type(text, delay=0)
        except Exception:
            # 键盘输入失败时整值 fill 兜底；fill 是替换语义，仅在
            # 一次性写入完整内容时才正确。
            try:
                if self.locator is not None:
                    self.locator.fill(text, timeout=10000)
                else:
                    self.handle.fill(text, timeout=10000)
            except Exception:
                pass

    @property
    def tag_name(self) -> str:
        try:
            return str(self._eval("el => el.tagName.toLowerCase()") or "")
        except Exception:
            return ""

    def input_value(self) -> str:
        """读取 input 元素当前值(cloak 下 get_attribute 偶发失真,优先原生接口)。"""
        if self.locator is not None:
            try:
                try:
                    return str(self.locator.input_value(timeout=3000) or "")
                except TypeError:
                    return str(self.locator.input_value() or "")
            except Exception:
                pass
        if self.handle is not None:
            try:
                return str(self.handle.input_value() or "")
            except Exception:
                pass
        return self.get_attribute("value") or ""

    def fill(self, text: str) -> None:
        """整体填值并触发 input 事件(比逐字输入可靠,无丢字风险)。"""
        if self.locator is not None:
            try:
                try:
                    self.locator.fill(text, timeout=5000)
                    return
                except TypeError:
                    self.locator.fill(text)
                    return
            except Exception:
                pass
        if self.handle is not None:
            try:
                self.handle.fill(text)
                return
            except Exception:
                pass
        try:
            self._eval(
                "(el, v) => { el.value = v;"
                " el.dispatchEvent(new Event('input', {bubbles: true}));"
                " el.dispatchEvent(new Event('change', {bubbles: true})); }",
                str(text),
            )
        except Exception:
            pass

    def scroll_into_view(self, block: str = "center") -> None:
        """滚动到元素可见(cloak 下 CloakElement 不能作为 execute_script 参数传递)。"""
        if self.locator is not None:
            try:
                try:
                    self.locator.scroll_into_view_if_needed(timeout=5000)
                    return
                except TypeError:
                    self.locator.scroll_into_view_if_needed()
                    return
            except Exception:
                pass
        if self.handle is not None:
            try:
                self.handle.scroll_into_view_if_needed()
            except Exception:
                pass

    def focus(self) -> None:
        if self.locator is not None:
            try:
                try:
                    self.locator.focus(timeout=3000)
                    return
                except TypeError:
                    self.locator.focus()
                    return
            except Exception:
                pass
        if self.handle is not None:
            try:
                self.handle.focus()
            except Exception:
                pass

    def get_attribute(self, name: str) -> str | None:
        try:
            if self.locator is not None:
                return self.locator.get_attribute(name, timeout=1000)
            return self.handle.get_attribute(name)
        except Exception:
            return None


class _SwitchTo:
    def __init__(self, driver: "CloakSeleniumDriver"):
        self._driver = driver

    def window(self, handle: str) -> None:
        self._driver._switch_window(handle)

    @property
    def active_element(self) -> CloakElement:
        # Selenium 语义：当前持有焦点的元素；供 switch_to.active_element.send_keys 使用。
        handle = self._driver.page.evaluate_handle("() => document.activeElement")
        element = None
        try:
            element = handle.as_element()
        except Exception:
            element = None
        if element is None:
            raise RuntimeError("当前页面没有持有焦点的元素")
        return CloakElement(self._driver.page, handle=element)


class CloakSeleniumDriver:
    """只实现本项目共享页面操作流程(page_ops)实际用到的 WebDriver 子集。"""

    def __init__(self, browser: Any, context: Any | None, page: Any):
        self.browser = browser
        self.context = context
        self.page = page
        self._page_load_timeout_ms = int(getattr(_cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90) * 1000
        self.switch_to = _SwitchTo(self)

    @property
    def current_url(self) -> str:
        return str(getattr(self.page, "url", "") or "")

    @property
    def window_handles(self) -> list[str]:
        pages = self._pages()
        return [str(i) for i in range(len(pages))]

    def _pages(self) -> list[Any]:
        try:
            if self.context is not None:
                return list(self.context.pages)
        except Exception:
            pass
        try:
            contexts = list(getattr(self.browser, "contexts", []) or [])
            pages = []
            for ctx in contexts:
                pages.extend(list(getattr(ctx, "pages", []) or []))
            return pages or [self.page]
        except Exception:
            return [self.page]

    def _switch_window(self, handle: str) -> None:
        pages = self._pages()
        idx = int(handle)
        self.page = pages[idx]
        try:
            self.page.bring_to_front()
        except Exception:
            pass

    def set_page_load_timeout(self, seconds: int) -> None:
        self._page_load_timeout_ms = int(seconds) * 1000
        try:
            self.page.set_default_navigation_timeout(self._page_load_timeout_ms)
            self.page.set_default_timeout(self._page_load_timeout_ms)
        except Exception:
            pass

    def get(self, url: str) -> None:
        # commit: 收到响应即返回。domcontentloaded 在部分代理出口上永远不触发,
        # goto 会一直阻塞(超时参数也不生效), 注册线程直接卡死
        # (实测 2026-09-22 服务器: 打开登录页后再无日志)。
        self.page.goto(url, wait_until="commit", timeout=self._page_load_timeout_ms)

    def back(self) -> None:
        self.page.go_back(wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def refresh(self) -> None:
        self.page.reload(wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def quit(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        except Exception:
            pass
        try:
            self.browser.close()
        except Exception:
            pass
        finally:
            # quit 后无条件脱管并清扫: close "成功"不代表进程退出
            # (实测 chrome 残留 + node driver 永不退, 每组漏 ~500MB)
            _ACTIVE_DRIVER_IDS.discard(id(self))
            try:
                sweep_orphan_browsers()
            except Exception:
                pass

    def find_elements(self, by: Any, selector: str) -> list[CloakElement]:
        loc = self._locator(by, selector)
        try:
            count = min(int(loc.count()), 200)
        except Exception:
            count = 0
        return [CloakElement(self.page, loc.nth(i)) for i in range(count)]

    def find_element(self, by: Any, selector: str) -> CloakElement:
        els = self.find_elements(by, selector)
        if not els:
            raise RuntimeError(f"找不到页面元素: {selector}")
        return els[0]

    def _locator(self, by: Any, selector: str):
        by_s = str(by or "").lower()
        if "xpath" in by_s or str(selector).startswith("//"):
            return self.page.locator(f"xpath={selector}")
        return self.page.locator(selector)

    def execute_script(self, script: str, *args: Any) -> Any:
        return self._evaluate(script, args=args, async_mode=False)

    def execute_async_script(self, script: str, *args: Any) -> Any:
        return self._evaluate(script, args=args, async_mode=True)

    def execute_cdp_cmd(self, cmd: str, params: dict | None = None) -> Any:
        params = params or {}
        try:
            client = self.context.new_cdp_session(self.page) if self.context is not None else self.page.context.new_cdp_session(self.page)
            return client.send(cmd, params)
        except Exception as exc:
            logger.debug("[Cloak] CDP 命令失败 %s: %s", cmd, exc)
            return None

    def _serialize_args(self, args: tuple[Any, ...]) -> tuple[CloakElement | None, list[Any]]:
        """拆分 Selenium 脚本参数。

        Playwright 的 JSHandle/ElementHandle 不能可靠地嵌在 dict/list payload 中跨
        page.evaluate 传递；Selenium 脚本最常见模式是 `arguments[0]` 为元素，
        因此这里把第一个 CloakElement 作为真实 DOM `el` 传入，其它参数保持
        JSON 可序列化。
        """
        first_el = args[0] if args and isinstance(args[0], CloakElement) else None
        rest = list(args[1:] if first_el else args)
        cleaned = []
        for item in rest:
            if isinstance(item, CloakElement):
                # 极少数脚本会传多个元素；用真实 handle 直接会在嵌套 payload 中失效，
                # 这里退化为 None，比把错误对象传进 JS 更安全。
                cleaned.append(None)
            else:
                cleaned.append(item)
        return first_el, cleaned

    @staticmethod
    def _unwrap_js_result(page, handle: Any) -> Any:
        try:
            element = handle.as_element()
        except Exception:
            element = None
        if element is not None:
            return CloakElement(page, handle=element)
        try:
            return handle.json_value()
        except Exception as exc:
            msg = str(exc)
            if "Execution context was destroyed" in msg or "navigation" in msg.lower():
                logger.info("[Cloak] JS 执行后页面发生跳转，忽略返回值读取失败：%s", msg[:160])
                return {"ok": True, "reason": "navigation_after_script"}
            raise
        finally:
            try:
                handle.dispose()
            except Exception:
                pass

    def _evaluate(self, script: str, args: tuple[Any, ...], async_mode: bool) -> Any:
        first_el, serial_args = self._serialize_args(args)
        if async_mode:
            wrapper = """async ({script, args}) => {
              return await new Promise((resolve) => {
                const fn = new Function(...args.map((_, i) => 'a' + i), '__cloak_done', script);
                const timer = setTimeout(() => resolve({__cloak_timeout:true}), 120000);
                const __cloak_done = (v) => { clearTimeout(timer); resolve(v); };
                try { fn(...args, __cloak_done); } catch (e) { clearTimeout(timer); resolve({ok:false, error:String(e)}); }
              });
            }"""
            element_wrapper = """async (el, payload) => {
              const args = [el, ...payload.args];
              return await new Promise((resolve) => {
                const fn = new Function(...args.map((_, i) => 'a' + i), '__cloak_done', payload.script);
                const timer = setTimeout(() => resolve({__cloak_timeout:true}), 120000);
                const __cloak_done = (v) => { clearTimeout(timer); resolve(v); };
                try { fn(...args, __cloak_done); } catch (e) { clearTimeout(timer); resolve({ok:false, error:String(e)}); }
              });
            }"""
            if first_el is not None:
                result = first_el._eval(element_wrapper, {"script": script, "args": serial_args})
            else:
                result = self.page.evaluate(wrapper, {"script": script, "args": serial_args})
            if isinstance(result, dict) and result.get("__cloak_timeout"):
                raise TimeoutError("execute_async_script timeout")
            return result

        # Selenium 脚本经常以 `return ...` 为主体；用 Function 保持语义。
        wrapper = """({script, args}) => {
          const fn = new Function(...args.map((_, i) => 'a' + i), script);
          return fn(...args);
        }"""
        element_wrapper = """(el, payload) => {
          const args = [el, ...payload.args];
          const fn = new Function(...args.map((_, i) => 'a' + i), payload.script);
          return fn(...args);
        }"""
        if first_el is not None:
            handle = first_el._eval_handle(element_wrapper, {"script": script, "args": serial_args})
        else:
            handle = self.page.evaluate_handle(wrapper, {"script": script, "args": serial_args})
        return self._unwrap_js_result(self.page, handle)


def _normalize_proxy(proxy: str | None) -> str | None:
    proxy = str(proxy or "").strip()
    if not proxy:
        return None
    normalized = proxy.replace("socks5h://", "socks5://")
    # Chromium 不支持 SOCKS5 认证(--proxy-server 的 socks5 user:pass 会被
    # 静默忽略,表现为代理连通但 407/无出口):带凭据的粘性住宅代理必须用
    # http(s) 形式。这里只告警不改写,让问题在日志里显形。
    if normalized.startswith("socks5://") and "@" in normalized.split("://", 1)[-1]:
        logger.warning(
            "[Cloak] SOCKS5 代理带认证参数,Chromium 内核不支持 SOCKS 认证,浏览器侧将无法通过代理验证(建议改用 http 形式): %s",
            proxy.split("@")[-1],
        )
    return normalized


def _detect_cloak_exit_geo(proxy_url: str | None = None) -> dict:
    """按当前/代理出口检测地理信息，供 Cloak 显式 locale/timezone 使用。"""
    try:
        import requests
        from config import browser as _browser_cfg
        endpoints = list(getattr(_browser_cfg, "IP_GEO_ENDPOINTS", []) or [])
        timeout = float(getattr(_browser_cfg, "IP_GEO_TIMEOUT", 6) or 6)
    except Exception:
        return {}
    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    for url in endpoints:
        try:
            resp = requests.get(url, headers=headers, proxies=proxies, timeout=timeout)
            if resp.status_code != 200:
                continue
            data = resp.json()
            timezone = data.get("timezone")
            if isinstance(timezone, dict):
                timezone = timezone.get("id") or timezone.get("name")
            geo = {
                "ip": data.get("ip") or data.get("query"),
                "country": (data.get("country") or data.get("country_code") or data.get("countryCode") or "").upper(),
                "region": data.get("region") or data.get("regionName"),
                "city": data.get("city"),
                "timezone": timezone or "",
                "org": data.get("org") or data.get("isp") or (data.get("connection") or {}).get("org"),
            }
            if geo.get("country") or geo.get("timezone"):
                logger.info(
                    "[Cloak] 出口IP地理信息：ip=%s country=%s city=%s timezone=%s",
                    geo.get("ip") or "?", geo.get("country") or "?", geo.get("city") or "?", geo.get("timezone") or "?",
                )
                return geo
        except Exception as exc:
            logger.debug("[Cloak] 出口 IP 地理检测失败 endpoint=%s: %s: %s", url, type(exc).__name__, exc)
    return {}


def _build_cloak_locale_options(proxy_url: str | None = None) -> dict:
    """生成 Cloak/Playwright 双层语言时区配置。"""
    explicit_locale = str(getattr(_cfg, "CLOAK_LOCALE", "") or "").strip()
    explicit_timezone = str(getattr(_cfg, "CLOAK_TIMEZONE", "") or "").strip()
    out = {}
    if explicit_locale:
        out["locale"] = explicit_locale
        # Accept-Language 用 config.browser 自动推断更完整；显式时给一个保守值。
        out["accept_language"] = f"{explicit_locale},{explicit_locale.split('-')[0]};q=0.9,en-US;q=0.8,en;q=0.7"
    if explicit_timezone:
        out["timezone"] = explicit_timezone
    if explicit_locale and explicit_timezone:
        return out
    if not bool(getattr(_cfg, "CLOAK_GEOIP", True)):
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
        logger.debug("[Cloak] 构建自动语言/时区失败：%s: %s", type(exc).__name__, exc)
    return {k: v for k, v in out.items() if v}


def _scan_chromium_user_data_dirs() -> set:
    """扫描 /proc，返回当前所有 chromium 进程的 user-data-dir 标记（仅 POSIX）。"""
    markers: set = set()
    if os.name != "posix":
        return markers
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().decode("utf-8", "replace")
            except Exception:
                continue
            if "chrom" not in cmd.lower():
                continue
            for part in cmd.split("\x00"):
                if part.startswith("--user-data-dir=") and len(part) > 20:
                    markers.add(part.split("=", 1)[1])
    except Exception:
        pass
    return markers


def force_kill_browser(driver) -> int:
    """quit 超时后的兜底：按 user-data-dir 标记 SIGKILL 浏览器进程树。

    泄漏的 Chromium 每个占 200-500MB 且永远活着（实测 2026-09-22：容器过夜
    涨到 20G）。quit 挂死被放弃时必须强杀，否则批量重试一晚能泄漏几十个。
    返回杀掉的标记数。仅 POSIX（容器/Linux 宿主）生效。
    """
    if os.name != "posix":
        return 0
    markers = list(set(getattr(driver, "_kill_markers", None) or []))
    if not markers:
        return 0
    killed = 0
    try:
        pids = _pids_by_cmdline(tuple(f"--user-data-dir={m}" for m in markers))
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except Exception:
                pass
    except Exception as exc:
        logger.warning("[Cloak] 强杀浏览器进程失败: %s: %s", type(exc).__name__, str(exc)[:120])
    if killed:
        logger.warning("[Cloak] quit 超时/失败，已按标记强杀 %d 组浏览器进程", killed)
    _ACTIVE_DRIVER_IDS.discard(id(driver))
    try:
        sweep_orphan_browsers()
    except Exception:
        pass
    return killed


# 活跃浏览器实例(quit 后移除)。清扫孤儿时若集合非空说明有并发任务
# 在用浏览器, 只允许按各自 marker 清理, 不允许一锅端。
_ACTIVE_DRIVER_IDS: set[int] = set()


def release_driver(driver) -> None:
    """主线程脱管浏览器实例(不依赖 quit 内部 finally——quit 线程可能
    在 context.close 上挂死被留守, finally 永远执行不到)。"""
    try:
        _ACTIVE_DRIVER_IDS.discard(id(driver))
    except Exception:
        pass


def _pids_by_cmdline(patterns: tuple[str, ...]) -> list[int]:
    """纯 Python 扫 /proc 找命令行含任一模式的进程(容器里没有 pkill,
    subprocess 调 pkill 会 FileNotFoundError 被静默——2026-09-23 实测清扫
    从第一天起就是空转)。"""
    if os.name != "posix":
        return []
    me = os.getpid()
    pids: list[int] = []
    try:
        entries = os.listdir("/proc")
    except Exception:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me:
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except Exception:
            continue
        if any(pat in cmd for pat in patterns):
            pids.append(pid)
    return pids


def sweep_orphan_browsers(force: bool = False) -> int:
    """清扫孤儿浏览器进程(chrome + playwright node driver), 纯 os.kill 实现。

    泄漏实测(2026-09-23): quit() 的 context.close/browser.close 在挂起状态下
    "成功返回"但 chrome 进程不退, playwright node driver(每个 ~130MB)从不退出;
    每组任务漏 ~500MB, 堆到 mem_limit 触发 OOM, 新浏览器被 SIGKILL
    (TargetClosedError)。仅当无活跃实例时一锅端; force=True 供手动兜底。
    """
    if os.name != "posix":
        return 0
    if _ACTIVE_DRIVER_IDS and not force:
        return 0
    pids = _pids_by_cmdline(("chrome", "playwright/driver/node"))
    killed = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except Exception:
            pass
    if killed:
        logger.warning("[Cloak] 已清扫 %d 个孤儿浏览器进程(chrome/node driver)", killed)
    return killed


def build_cloak_driver(proxy: str | None = None) -> tuple[CloakSeleniumDriver, CloakOpenResult]:
    """启动 CloakBrowser 并返回 Selenium 风格 driver。

    proxy=None  时按 config.proxy.PROXY_POOL 随机抽取；
    proxy=""    时显式禁用代理；
    proxy="..." 时使用指定代理。
    """
    if proxy is None and bool(getattr(_cfg, "CLOAK_USE_PROXY", True)):
        try:
            from config.proxy import pick_proxy
            proxy = pick_proxy()
        except Exception:
            proxy = None
    try:
        from cloakbrowser import launch, launch_persistent_context
    except ImportError as exc:
        raise RuntimeError("未安装 cloakbrowser，请执行：pip install cloakbrowser") from exc

    launch_args = list(getattr(_cfg, "CLOAK_EXTRA_ARGS", []) or [])
    seed = str(getattr(_cfg, "CLOAK_FINGERPRINT_SEED", "") or "").strip()
    if seed:
        launch_args.append(f"--fingerprint={seed}")

    proxy_url = _normalize_proxy(proxy) if bool(getattr(_cfg, "CLOAK_USE_PROXY", True)) else None
    locale_opts = _build_cloak_locale_options(proxy_url)
    # geoip=True 交给 CloakBrowser 根据当前出口 IP 自动匹配 timezone/locale/WebRTC。
    # 之前只有显式 proxy_url 时才开启；如果用户走系统代理/VPN/透明代理，代码层面
    # 看不到 proxy_url，会误关 geoip，导致语言/时区不跟随出口。这里改为完全尊重配置。
    opts = {
        "headless": bool(getattr(_cfg, "CLOAK_HEADLESS", False)),
        "humanize": bool(getattr(_cfg, "CLOAK_HUMANIZE", True)),
        "geoip": bool(getattr(_cfg, "CLOAK_GEOIP", True)),
    }
    if locale_opts.get("locale"):
        opts["locale"] = locale_opts["locale"]
    if locale_opts.get("timezone"):
        opts["timezone"] = locale_opts["timezone"]
    if proxy_url:
        opts["proxy"] = proxy_url
    if launch_args:
        opts["args"] = launch_args
    license_key = str(getattr(_cfg, "CLOAK_LICENSE_KEY", "") or "").strip()
    if license_key:
        opts["license_key"] = license_key

    user_data_dir = str(getattr(_cfg, "CLOAK_USER_DATA_DIR", "") or "").strip()
    logger.info(
        "[Cloak] 启动 CloakBrowser：headless=%s humanize=%s geoip=%s proxy=%s locale=%s timezone=%s accept_language=%s persistent=%s",
        opts.get("headless"), opts.get("humanize"), opts.get("geoip"),
        proxy_url or "无", opts.get("locale") or "自动/默认", opts.get("timezone") or "自动/默认",
        locale_opts.get("accept_language") or "自动/默认", bool(user_data_dir),
    )
    context_kwargs = {}
    if locale_opts.get("locale"):
        context_kwargs["locale"] = locale_opts["locale"]
    if locale_opts.get("timezone"):
        context_kwargs["timezone_id"] = locale_opts["timezone"]
    if locale_opts.get("accept_language"):
        context_kwargs["extra_http_headers"] = {"Accept-Language": locale_opts["accept_language"]}

    _BEFORE_LAUNCH_MARKERS = _scan_chromium_user_data_dirs()
    try:
        if user_data_dir:
            context = launch_persistent_context(user_data_dir, **opts)
            page = context.new_page()
            browser = getattr(context, "browser", None) or context
            # persistent context 的 locale/timezone 已通过 launch_persistent_context 参数传入。
        else:
            browser = launch(**opts)
            context = browser.new_context(**context_kwargs)
            page = context.new_page()
    except Exception:
        # 启动失败(OOM/代理断)时浏览器可能半起: 按新出现的 user-data-dir 杀掉再抛
        try:
            _pids = _pids_by_cmdline(tuple(
                f"--user-data-dir={_m}"
                for _m in (_scan_chromium_user_data_dirs() - _BEFORE_LAUNCH_MARKERS)
            ))
            for _pid in _pids:
                try:
                    os.kill(_pid, signal.SIGKILL)
                except Exception:
                    pass
        except Exception:
            pass
        raise

    driver = CloakSeleniumDriver(browser=browser, context=context, page=page)
    _ACTIVE_DRIVER_IDS.add(id(driver))
    # 记录本实例浏览器的 user-data-dir 标记：quit 超时被放弃时按标记强杀，
    # 防止 Chromium 进程泄漏(每个 200-500MB，过夜可堆积数十 GB)。
    try:
        driver._kill_markers = list(_scan_chromium_user_data_dirs() - _BEFORE_LAUNCH_MARKERS)
    except Exception:
        driver._kill_markers = []
    # 共享页面操作函数(page_ops)需要一个显式日志前缀，
    # 避免 Cloak 注册流程里出现 `[Roxy注册]`。
    driver._registration_log_prefix = "[Cloak注册]"
    driver.set_page_load_timeout(int(getattr(_cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90))
    return driver, CloakOpenResult(raw={"driver": "cloakbrowser", "proxy": proxy_url, "locale": locale_opts, "options": {k: v for k, v in opts.items() if k != "license_key"}})
