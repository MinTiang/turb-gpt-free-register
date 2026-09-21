#!/bin/sh
# WebUI 入口:HOST/PORT 走环境变量(WEBUI_HOST/WEBUI_PORT),默认全网卡:5000
#
# 浏览器二进制不在启动时下载(不阻塞服务启动,启动秒级):
#   - cloakbrowser 由库自身在首跑检测 .cloakbrowser 卷缓存,没有才下载
#   - patchright 的 chromium 在首次注册真正用到时按需下载
#     (core/patchright_driver._ensure_chromium,落 /opt/ms-playwright 卷)
set -e
cd /app
exec python web.py --host "${WEBUI_HOST:-0.0.0.0}" --port "${WEBUI_PORT:-5000}"
