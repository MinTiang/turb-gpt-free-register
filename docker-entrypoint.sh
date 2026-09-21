#!/bin/sh
# WebUI 入口:HOST/PORT 走环境变量(WEBUI_HOST/WEBUI_PORT),默认全网卡:5000
#
# 浏览器二进制按需下载:playwright/patchright 的 chromium 不进镜像,挂在
# /opt/ms-playwright 卷上。卷为空(只有 .keep 占位)时才拉取,已存在则跳过
# (cloakbrowser 的二进制由库自身在首跑时做同样的缓存检测,落在 .cloakbrowser 卷)。
set -e
cd /app

pw_files=$(ls -A /opt/ms-playwright 2>/dev/null | grep -v '^\.keep$' || true)
if [ -z "$pw_files" ]; then
    echo "[entrypoint] 浏览器目录为空,下载 chromium(playwright + patchright)..."
    patchright install chromium
    playwright install chromium
else
    echo "[entrypoint] 浏览器二进制已存在,跳过下载"
fi

exec python web.py --host "${WEBUI_HOST:-0.0.0.0}" --port "${WEBUI_PORT:-5000}"
