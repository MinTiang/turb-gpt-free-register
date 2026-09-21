# -*- coding: utf-8 -*-
"""Clash 节点管理配置:自动切换/质量检测/冷却/单节点限额。

用于协议注册的出口管理:注册前自动挑选"质量良好+未冷却+未超限"的节点,
质量不佳或不支持 OpenAI 时自动换下一个;节点用后进入冷却。
全部参数 WebUI 可配,保存即生效(config 热重载)。
"""
from config.env_loader import apply_env_overrides

# Clash(mihomo) external-controller API 地址
CLASH_API_URL: str = "http://127.0.0.1:9097"
# API 密钥(留空=无鉴权)
CLASH_API_SECRET: str = "set-your-secret"
# 本地代理端口(出口质量检测走它)
CLASH_PROXY_PORT: int = 7897
# 切换目标分组名(Selector)
CLASH_SWITCH_GROUP: str = "🚀 手动切换"

# 注册流程的节点自动管理总开关(WebUI 任务与批量器共用语义):
# True = 任务占 worker 槽位走独立出口+自动选点;False = 走 PROXY_POOL 原行为
CLASH_AUTO_SWITCH_ENABLED: bool = False
# 注册前对候选节点做质量检测(IP 可达 + chatgpt.com 不被 403)
CLASH_QUALITY_CHECK_ENABLED: bool = True
# 质量检测结果的信任时长(小时);超过则复检
CLASH_QUALITY_TTL_HOURS: float = 6.0
# 节点被用于注册后的冷却时长(小时);冷却期内不再选用
CLASH_NODE_COOLDOWN_HOURS: float = 12.0
# 质量检测判 bad 后的跳过窗口(小时);窗口内选点直接排除不再复测。
# 0 = 关闭(恢复旧行为:bad 节点每个任务重新探测)。CF 按 IP 段封禁通常持续
# 数小时,复测纯烧时间(实测一个 bad 节点 12-30s)。
CLASH_BAD_NODE_SKIP_HOURS: float = 3.0
# 单节点累计注册账号上限;达到后不再选用(可在页面重置)
CLASH_MAX_ACCOUNTS_PER_NODE: int = 2

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'CLASH_API_URL': 'str',
    'CLASH_API_SECRET': 'str',
    'CLASH_PROXY_PORT': 'int',
    'CLASH_SWITCH_GROUP': 'str',
    'CLASH_AUTO_SWITCH_ENABLED': 'bool',
    'CLASH_QUALITY_CHECK_ENABLED': 'bool',
    'CLASH_QUALITY_TTL_HOURS': 'float',
    'CLASH_NODE_COOLDOWN_HOURS': 'float',
    'CLASH_BAD_NODE_SKIP_HOURS': 'float',
    'CLASH_MAX_ACCOUNTS_PER_NODE': 'int',
})
