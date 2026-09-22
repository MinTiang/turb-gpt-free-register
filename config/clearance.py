# -*- coding: utf-8 -*-
"""Cloudflare clearance 配置（FlareSolverr）。

auth.openai.com / chatgpt.com 对注册/授权链路做概率性 CF 拦截（免费节点池
实测通过率约 50%）。开启本项后，被拦时用 FlareSolverr 真实解一次质询，
把 cf_clearance + __cf_bm cookie 与匹配 UA 注入后续请求，显著降低拦截率。

部署 FlareSolverr（Docker）：
    docker run -d --name flaresolverr --restart unless-stopped \
        -p 8191:8191 flaresolverr/flaresolverr:latest

如果 turb 自身跑在容器里，容器内访问宿主机服务用 host.docker.internal。
"""
from config.env_loader import apply_env_overrides

# none = 关闭（默认，保持原行为）；flaresolverr = 启用
CLEARANCE_MODE: str = "none"

# FlareSolverr 服务地址（容器内访问宿主机时用 http://host.docker.internal:8191）
CLEARANCE_FLARESOLVERR_URL: str = "http://127.0.0.1:8191"

# 单次解质询超时（秒）；FlareSolverr 会自动重试，实际可能更久
CLEARANCE_TIMEOUT_SEC: int = 60

# 解出的 clearance 缓存时长（秒）；cf_clearance 有效期通常 30 分钟
CLEARANCE_TTL_SEC: int = 900

# 传给 FlareSolverr 的代理地址（留空=FlareSolverr 直连）。
# 本机部署时填 Clash 混合端口，例如 http://127.0.0.1:7897
# turb 在容器里而 FlareSolverr 也在容器里时，填 http://host.docker.internal:7897
CLEARANCE_PROXY_URL: str = ""

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'CLEARANCE_MODE': 'str',
    'CLEARANCE_FLARESOLVERR_URL': 'str',
    'CLEARANCE_TIMEOUT_SEC': 'int',
    'CLEARANCE_TTL_SEC': 'int',
    'CLEARANCE_PROXY_URL': 'str',
})
