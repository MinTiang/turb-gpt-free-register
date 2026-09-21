# syntax=docker/dockerfile:1
# turb-gpt-free-register WebUI 容器镜像
# 运行态(turb.sqlite3/日志/浏览器缓存)全部落卷,镜像只含代码与浏览器二进制。
# 部署说明见 docs/DEPLOY_DOCKER.md。
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    # 浏览器二进制不进镜像:首启由 entrypoint 检测卷空才下载(挂卷持久化)
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    # cloakbrowser 首跑自下载二进制到 $HOME/.cloakbrowser(compose 挂卷持久化)
    HOME=/home/app \
    WEBUI_HOST=0.0.0.0 \
    WEBUI_PORT=5000

WORKDIR /app

# 国内网络构建慢/502 时:docker build --build-arg APT_MIRROR=mirrors.tuna.tsinghua.edu.cn .
# (compose 里 APT_MIRROR 环境变量透传;GH Actions 走默认官方源)
ARG APT_MIRROR=deb.debian.org

# Node.js: sentinel 令牌走 node sentinel-runner.js(core/sentinel_runner.py)
# ca-certificates/curl: 启动期下载浏览器二进制、cloakbrowser 地理库用。
# gosu: entrypoint 修正 bind 目录属主后降权到 app 用户。
# 只装系统级依赖(apt 层):浏览器二进制留给首次使用时按需下载。
COPY requirements.txt ./
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl nodejs gosu \
    && pip install --no-cache-dir -r requirements.txt \
    && playwright install-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# 运行态集中到 /app/state(挂卷):sqlite 主库经符号链接落到卷里,避免文件级
# bind mount 首次启动被 docker 建成目录的经典坑。
# /opt/ms-playwright 放 .keep 占位:首启卷内容为空(仅占位)时 entrypoint 才下载浏览器。
RUN mkdir -p /app/state /app/data /app/logs /app/run /app/注册日志 /home/app/.cloakbrowser \
    && mkdir -p /opt/ms-playwright && touch /opt/ms-playwright/.keep \
    && chmod +x /app/docker-entrypoint.sh \
    && ln -sf /app/state/turb.sqlite3 /app/turb.sqlite3 \
    && useradd -m -u 1000 app \
    && chown -R app:app /app /home/app /opt/ms-playwright
# 不写 USER app:entrypoint 以 root 起步修正 bind 目录属主(Linux 宿主机上
# docker 建的挂载目录是 root 属主,uid 1000 直写会 sqlite unable to open),
# 修正后经 gosu 降权到 app 运行服务。

EXPOSE 5000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
