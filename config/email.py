# -*- coding: utf-8 -*-
"""
邮箱服务配置。

Outlook 注册邮箱与 OTP 的默认池行为：
    1. 首次启动会把旧的 `用于注册的邮箱.txt` 迁移到 SQLite
    2. 运行期间通过 WebUI「邮箱库」导入和管理邮箱
    3. 注册时直接从 SQLite 邮箱库领取可用邮箱

另保留 Cloudflare Worker 临时邮箱（EMAIL_SOURCE 含 cloudflare 时启用）：
自建 Worker 可无限新建地址，用于测试/补充来源。
"""
from config.env_loader import env_str, apply_env_overrides


# True: REGISTER_EMAIL 留空时从邮箱池自动获取邮箱，OTP 自动收取
# False: 走人工输入邮箱 + 人工填 OTP 的流程
USE_EMAIL_SERVICE = False

# 邮箱来源。支持单个或多个（逗号分隔，按顺序兜底）：
#   outlook    = 外购 Outlook 账号池 + mail.chatai.codes 远端取信
#   cloudflare = 自建 Cloudflare Worker 临时邮箱（可无限新建）
# 历史上支持过的 cloudflare_domain / generic_api / imap / gptmail /
# mailnest / cloudmail / remail 客户端已删除，配置里残留的旧值会被忽略。
EMAIL_SOURCE = "outlook"


# ============================================================
# Outlook 模式（外购账号池 + 取信服务）
# ============================================================

OUTLOOK_ACCOUNTS_FILE = "用于注册的邮箱.txt"

# Outlook 取件模式：
#   "auto"   = 先用远端 mail.chatai.codes；远端 402/DEPLOYMENT_DISABLED 时自动切 Microsoft Graph 直连
#   "remote" = 只用远端 mail.chatai.codes
#   "direct" = 只用 Microsoft Graph 直连（使用 clientId + refreshToken 换 access_token）
OUTLOOK_FETCH_MODE = "auto"

# 取邮件 API 的根 URL（远端模式使用）
OUTLOOK_API_BASE = "https://mail.chatai.codes"


# ============================================================
# Cloudflare Worker 临时邮箱（EMAIL_SOURCE 含 cloudflare 时启用）
# 自建 Worker：可无限新建地址，OTP 从 Worker 的 /api/mails 拉取。
# ============================================================

CLOUDFLARE_API_BASE = "https://mail.131518.xyz"
CLOUDFLARE_API_KEY = ""
# x-admin-auth / bearer / x-api-key / query-key
CLOUDFLARE_AUTH_MODE = "x-admin-auth"
CLOUDFLARE_CUSTOM_AUTH = ""
CLOUDFLARE_PATH_ACCOUNTS = "/admin/new_address"
CLOUDFLARE_PATH_MESSAGES = "/api/mails"
CLOUDFLARE_DEFAULT_DOMAINS = ""
CLOUDFLARE_REQUEST_TIMEOUT = 20
CLOUDFLARE_NAME_LENGTH = 10


# ============================================================
# OTP 轮询参数
# ============================================================

OTP_POLL_INTERVAL = 3
OTP_MAX_WAIT = 90

# Outlook 双协议取件：抓到一封 OTP 后再多等多少秒看是否有更晚到达的邮件。
OTP_SETTLE_SECONDS = 5


# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'USE_EMAIL_SERVICE': 'bool', 'OTP_MAX_WAIT': 'int', 'OTP_POLL_INTERVAL': 'int',
    'EMAIL_SOURCE': 'str', 'OUTLOOK_FETCH_MODE': 'str',
    'CLOUDFLARE_API_BASE': 'str', 'CLOUDFLARE_API_KEY': 'str', 'CLOUDFLARE_AUTH_MODE': 'str',
    'CLOUDFLARE_CUSTOM_AUTH': 'str', 'CLOUDFLARE_PATH_ACCOUNTS': 'str', 'CLOUDFLARE_PATH_MESSAGES': 'str',
    'CLOUDFLARE_DEFAULT_DOMAINS': 'str', 'CLOUDFLARE_REQUEST_TIMEOUT': 'int', 'CLOUDFLARE_NAME_LENGTH': 'int',
})
