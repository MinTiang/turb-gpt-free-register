# Docker Compose 部署

镜像发布:push **`v*` 开头的标签**(如 `v0.0.1`)时,GitHub Actions 自动构建
amd64/arm64 双平台并合成同一个多架构标签,推送 `ghcr.io/mintiang/turb-gpt-free-register`
(标签:`latest` + `v*` 版本号,`docker pull` 自动选架构;main 分支推送不触发构建)。

## 镜像内容

- 基础:`python:3.13-slim` + Node.js(sentinel 令牌的 `sentinel-runner.js` 需要)+ Chromium 系统依赖(apt)
- **浏览器二进制不进镜像、启动时也不下载**(服务秒级启动):cloakbrowser 由库自身
  在首跑检测 `.cloakbrowser` 卷缓存,其 Playwright 二进制落 `/opt/ms-playwright` 卷
- 非 root 运行(uid 1000);运行态全部落卷,镜像无状态

## 卷一览

| 卷 | 容器路径 | 内容 |
|---|---|---|
| `app-state` | `/app/state` | `turb.sqlite3` 主库(镜像内已做符号链接) |
| `app-data` | `/app/data` | 运行时数据目录 |
| `app-logs` | `/app/logs` | WebUI 运行日志 |
| `app-reglogs` | `/app/注册日志` | 每任务注册日志 |
| `app-cloak` | `/home/app/.cloakbrowser` | cloakbrowser 浏览器二进制缓存(首跑下载) |
| `app-pw` | `/opt/ms-playwright` | Playwright 浏览器二进制(CloakBrowser 使用) |
| bind | `/app/.env` | 配置(WebUI 配置页写回,**勿加 `:ro`**) |

## 部署步骤

1. **改 .env(容器关键项)**——容器里的 `127.0.0.1` 是容器自己,凡是要访问宿主机
   本地代理的地址一律改成 `host.docker.internal`:

   ```ini
   PROXY_POOL="socks5://host.docker.internal:7897"
   ```

2. **宿主机本地代理放行容器访问**:代理监听地址需允许 LAN(如 7897 混合端口
   监听 0.0.0.0),否则容器无法连出。

3. **启动**:

   ```bash
   docker compose up -d        # 本地无镜像时自动构建;也可 compose pull 拉 GHCR
   docker compose logs -f      # 启动秒级;浏览器二进制在首个注册任务使用时按需下载
   # WebUI: http://localhost:5000
   ```

4. **更新版本**:

   ```bash
   docker compose pull && docker compose up -d
   ```

## 注意事项

- **配置持久性**:配置页里落在 `.env` 的项经 bind 挂载持久保存(单文件 bind 挂载
  不可原子改名,保存时自动降级为就地覆写);落在 `config/*.py` 的项写在容器内,
  **重建/升级容器会丢**——重要配置一律用 .env 里的项

- **国内网络构建**:apt 官方源(Fastly)常 502,本地构建切清华源——
  `APT_MIRROR=mirrors.tuna.tsinghua.edu.cn docker compose build`(GH Actions 用默认源即可)

- **数据备份** = 备份 `app-state` 卷(主库)+ `.env`。导出:`docker run --rm -v turb-gpt-free-register_app-state:/d alpine tar cz -C /d .`
- cloakbrowser 为商业库,容器内首跑需要有效的 `CLOAK_LICENSE_KEY` 且能出网
- 镜像为多架构 manifest(`latest` + `v*`):amd64 原生构建,arm64 走 QEMU 模拟较慢
- WebUI 是敏感控制台,`5000` 端口勿直接暴露公网;远程访问建议走 Tailscale/内网或加反代认证
- 镜像内不含 `.env` 与任何运行数据;`WEBUI_AUTH_CODE` 务必设置
