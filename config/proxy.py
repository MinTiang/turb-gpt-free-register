# -*- coding: utf-8 -*-
"""
代理池配置

每次注册随机抽取一个代理，保证不同 sid 之间彼此独立，避免风控关联。

协议说明：
    - http:// / https://   HTTP(S) 代理
    - socks5://            SOCKS5（DNS 本地解析，可能泄漏）
    - socks5h://           SOCKS5（DNS 在代理端解析，推荐，避免 DNS-IP 错配）

粘性会话（sticky IP）住宅代理：
    - 池条目里写 {sid} 占位符，例如
        http://user-region-US-sid-{sid}:pass@gw.provider.com:8000
      每次注册实例化时 {sid} 替换为随机 8 位 hex——一号一出口 IP；
      同一次注册内协议会话与浏览器驱动共用同一个展开值，全程同 IP。
    - 或写 {email} 占位符,以邮箱作为粘性会话键(与 gpt-account-hub 对齐):
        socks5h://my.{email}:Password4!@resin.example.com:5178
      {email} 展开为 URL 转义后的邮箱(@ → %40),例如
        socks5h://my.viapapad052122%40outlook.com:Password4!@resin.example.com:5178
      代理网关解码后收到的用户名是完整邮箱;同一邮箱在 turb 注册与 hub
      使用两侧算出的 username 完全一致 → 粘性网关返回同一个出口 IP。
    - 浏览器通道(cloak)注意：Chromium 不支持 SOCKS5 认证,粘性代理
      请用 http(s) 形式。
"""
from config.env_loader import apply_env_overrides
import random
import uuid


# 本地代理入口；实际出口地区以代理/分流规则为准。
# 推荐使用 socks5h://（DNS 在代理端解析），避免本地 DNS 与出口 IP 地区错配。
# 支持 {sid} / {email} 粘性会话占位符（见模块 docstring）。
PROXY_POOL = [
    "socks5://127.0.0.1:7897",
]

# 粘性会话占位符：PROXY_POOL 条目中的 {sid} 在每次注册时展开为随机会话 ID
STICKY_PROXY_PLACEHOLDER = "{sid}"
# 邮箱会话占位符：{email} 展开为 URL 转义后的邮箱(@ → %40),与 hub 注入规则一致
STICKY_EMAIL_PLACEHOLDER = "{email}"

# 套餐/Plus 试用资格查询与 Codex Agent Token 生成共用这组独立网络策略，
# 避免批量请求被注册代理池中的临时本地代理拖垮，也避免无条件直连造成出口策略失控。
#   auto   = 优先使用 PLAN_CHECK_PROXY 或代理池；本地代理端口未监听时回退直连
#   proxy  = 强制使用 PLAN_CHECK_PROXY 或代理池，失败直接报错
#   direct = 始终直连
PLAN_CHECK_PROXY_MODE = "auto"

# 套餐查询 / Codex Agent Token 生成专用代理。留空时 auto/proxy 模式从 PROXY_POOL 选择。
# 代理可能包含账号密码，因此 WebUI 会把它保存到 .env。
PLAN_CHECK_PROXY = ""

# 查套餐 / 生成 Codex Agent Token 使用独立的短超时和有限重试，避免后台任务长时间卡住。
PLAN_CHECK_TIMEOUT = 15.0
PLAN_CHECK_MAX_ATTEMPTS = 3
PLAN_CHECK_RETRY_DELAY = 2.0

# 新注册账号的权益可能存在短暂同步延迟。首次查询失败，或返回 free 且暂未发现
# Plus 试用资格时，等待该秒数后再复查一次；设为 0 可关闭复查。
PLAN_CHECK_REGISTRATION_RECHECK_DELAY = 2.0

# 自动、手动和批量套餐查询共用同一个后台队列；Codex Agent Token 使用独立队列，
# 但复用这里的网络模式、请求启动间隔与随机抖动，避免批量后台请求过于集中。
PLAN_CHECK_WORKERS = 3
PLAN_CHECK_QUEUE_LIMIT = 500
PLAN_CHECK_MIN_INTERVAL = 1.0
PLAN_CHECK_JITTER = 0.8


def expand_sticky_proxy(proxy: str | None, email: str | None = None, variant: int = 0) -> str:
    """展开代理 URL 中的粘性会话占位符。

    - {sid}   → 本次运行专属的随机 8 位 hex(与 main.py 的 sid 日志打码一致)
    - {email} → URL 转义后的邮箱(@ → %40),**仅转义 @,不要改用 quote()**:
      quote 会额外转义 + 等字符,导致与 gpt-account-hub 侧注入的 username
      不一致,粘性网关会把同一邮箱分到不同出口。模板含 {email} 而 email
      为空时抛 ValueError(调用方须先确定邮箱再展开)。
    - variant > 0 时邮箱会话键变为 "邮箱-variant"(vgvoip31824-2@outlook.com),
      网关视为新会话分配新出口——注册遇到 IP 类失败时换出口重试用(最多换
      3 次,见 registration_service._STICKY_MAX_VARIANTS)。

    幂等：不含占位符的 URL（含已展开的）原样返回；"" 表示显式禁用代理，
    原样返回。
    """
    text = str(proxy or "")
    if not text:
        return text
    if STICKY_EMAIL_PLACEHOLDER in text:
        addr = str(email or "").strip()
        if not addr:
            raise ValueError("代理模板包含 {email} 占位符，但本次注册邮箱尚未确定")
        v = int(variant or 0)
        if v > 0:
            local, _, domain = addr.rpartition("@")
            if local:
                addr = f"{local}-{v}@{domain}"
        text = text.replace(STICKY_EMAIL_PLACEHOLDER, addr.replace("@", "%40"))
    if STICKY_PROXY_PLACEHOLDER in text:
        text = text.replace(STICKY_PROXY_PLACEHOLDER, uuid.uuid4().hex[:8])
    return text


def _expand_session_placeholder(proxy: str) -> str:
    """仅展开 {sid}——无上下文依赖,任何消费者(pick_proxy 等)都可安全调用。

    {email} 需要邮箱上下文,原样保留,由注册主流程经 expand_sticky_proxy 展开。
    """
    if STICKY_PROXY_PLACEHOLDER in proxy:
        return proxy.replace(STICKY_PROXY_PLACEHOLDER, uuid.uuid4().hex[:8])
    return proxy


def pick_proxy() -> str:
    """从代理池中随机抽取一个代理 URL 并展开 {sid} 占位符；池为空时返回空串（即不使用代理）。

    含 {email} 的条目原样返回（无法在无邮箱上下文时展开），由
    main.run_registration 入口展开——多消费者各自调用会得到不同 {sid}，
    注册主流程应在入口解析一次以保证全程同 IP。
    """
    return _expand_session_placeholder(random.choice(PROXY_POOL)) if PROXY_POOL else ""


# 兼容入口：默认每次进程启动随机选一个，作为本次注册全程的固定代理
PROXY = pick_proxy()

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'PROXY_POOL': 'list_str_multiline',
    'PLAN_CHECK_PROXY_MODE': 'str',
    'PLAN_CHECK_PROXY': 'str',
    'PLAN_CHECK_TIMEOUT': 'float',
    'PLAN_CHECK_MAX_ATTEMPTS': 'int',
    'PLAN_CHECK_RETRY_DELAY': 'float',
    'PLAN_CHECK_REGISTRATION_RECHECK_DELAY': 'float',
    'PLAN_CHECK_WORKERS': 'int',
    'PLAN_CHECK_QUEUE_LIMIT': 'int',
    'PLAN_CHECK_MIN_INTERVAL': 'float',
    'PLAN_CHECK_JITTER': 'float',
})
PROXY = pick_proxy()
