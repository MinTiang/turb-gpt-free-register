# -*- coding: utf-8 -*-
"""gpt-account-hub 账号推送配置。

注册成功后把账号(accessToken/密码/邮箱四件套/codex 令牌)推送到
gpt-account-hub 的接收接口(POST {base_url}/api/accounts/import)。
推送密钥使用 hub 的独立 hub_push_* 令牌,与管理端令牌互不影响。
"""
from config.env_loader import apply_env_overrides

# 推送总开关;False 时注册后不自动推,手动推送按钮也会提示未启用。
HUB_PUSH_ENABLED: bool = False

# hub 实例地址,如 http://127.0.0.1:8600 或 https://hub.example.com
HUB_PUSH_BASE_URL: str = ""

# hub 的推送密钥(hub_push_开头,在 hub 管理端「推送接入」页生成)
HUB_PUSH_API_KEY: str = ""

# 注册成功后自动推送(需同时 HUB_PUSH_ENABLED=True)
HUB_PUSH_ON_REGISTER: bool = True

# 推送请求超时与失败重试次数
HUB_PUSH_TIMEOUT: int = 20
HUB_PUSH_RETRY: int = 2

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'HUB_PUSH_ENABLED': 'bool',
    'HUB_PUSH_BASE_URL': 'str',
    'HUB_PUSH_API_KEY': 'str',
    'HUB_PUSH_ON_REGISTER': 'bool',
    'HUB_PUSH_TIMEOUT': 'int',
    'HUB_PUSH_RETRY': 'int',
})
