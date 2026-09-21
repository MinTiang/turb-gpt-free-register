#!/bin/sh
# WebUI 入口:HOST/PORT 走环境变量(WEBUI_HOST/WEBUI_PORT),默认全网卡:5000
#
# 浏览器二进制不在启动时下载(不阻塞服务启动,启动秒级):
#   - cloakbrowser 由库自身在首跑检测 .cloakbrowser 卷缓存,没有才下载
#   - patchright 的 chromium 在首次注册真正用到时按需下载
#     (core/patchright_driver._ensure_chromium,落 /opt/ms-playwright 卷)
set -e
cd /app

run_webui() {
    exec python web.py --host "${WEBUI_HOST:-0.0.0.0}" --port "${WEBUI_PORT:-5000}"
}

# root 起步时:先修正运行目录属主再降权到 app。
# Linux 宿主机上 docker 创建的 bind 挂载目录属主是 root,uid 1000 直写会
# sqlite "unable to open database file"——这里自愈,宿主机无需手工 chown。
if [ "$(id -u)" = "0" ]; then
    chown app:app \
        /app/state /app/data /app/logs /app/run /app/注册日志 \
        /home/app /home/app/.cloakbrowser /opt/ms-playwright /app/.env 2>/dev/null || true
    exec gosu app python web.py --host "${WEBUI_HOST:-0.0.0.0}" --port "${WEBUI_PORT:-5000}"
fi

run_webui
