# -*- coding: utf-8 -*-
"""
注册基础信息（默认值）

CLI 走 main.py 时会优先读这里；Web 控制台批量注册时也会用同样的默认值。
留空字段会触发交互式输入或自动生成（仅 USE_EMAIL_SERVICE=True 时邮箱会从 Outlook 池领取）。
"""
from config.env_loader import apply_env_overrides

# 注册驱动（精简后仅保留两种）：
#   "protocol" = 纯协议注册（curl_cffi + Sentinel/PoW）
#   "cloak"    = CloakBrowser 指纹浏览器 + Playwright 适配层
# 原 roxy / patchright / browser_use / skyvern 驱动已移除。
REGISTRATION_DRIVER: str = "protocol"

# 注册邮箱（留空 + USE_EMAIL_SERVICE=True 时从 Outlook 池领取）
REGISTER_EMAIL = ""

# 是否走密码注册分支（authorize 后切到 create-account/password 页,
# 先 POST user/register 提交密码再验证邮箱 OTP）。
# ⚠️ 2026-09-20 受控实验(docs/协议注册实验.md):协议通道走密码分支的账号
# 在 0.4-0.7h 内 100% 被清理(n=4,跨 IP/warmup 变量);无密码分支存活 2.9h+
# (n=1, exp-007)。user/register 端点是协议会话的强标记信号。协议通道默认
# 无密码;需要密码重登能力时,建议注册存活后经 password/add 补设密码。
# 浏览器通道(cloak)真实执行 user/register,不受此影响,可自行开启。
REGISTER_WITH_PASSWORD = False

# 注册密码（留空则按强度规则随机生成；协议与浏览器驱动注册共用）
REGISTER_PASSWORD = ""

# 用户名（注册完成后设置的显示名称，留空会自动生成 "Foo Bar" 形式）
# OpenAI 限制：name_invalid_chars —— 只允许字母和空格
REGISTER_NAME = ""

# 注册成功落库后是否自动查询套餐/Plus 资格。
# 关闭后不会在注册完成后立刻访问 backend-api/accounts/check，后续可在账号列表手动查询。
AUTO_PLAN_CHECK_AFTER_REGISTER = False

# 注册成功并拿到 accessToken 后，在浏览器里随机停留一段时间再关闭连接。
# 格式：最小秒,最大秒。设为 "0,0" 表示不额外停留。
POST_REGISTER_DWELL_SECONDS_RANGE = "18,45"

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'REGISTRATION_DRIVER': 'str',
    'REGISTER_EMAIL': 'str',
    'REGISTER_WITH_PASSWORD': 'bool',
    'REGISTER_NAME': 'str',
    'AUTO_PLAN_CHECK_AFTER_REGISTER': 'bool',
    'POST_REGISTER_DWELL_SECONDS_RANGE': 'str',
})
