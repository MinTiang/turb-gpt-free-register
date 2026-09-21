# Docker Compose 部署

镜像发布:push **`v*` 开头的标签**(如 `v0.0.1`)时,GitHub Actions 自动构建
amd64/arm64 两个平台的镜像并推送 `ghcr.io/mintiang/turb-gpt-free-register`,
平台专属标签:`v0.0.1-amd64` / `latest-amd64` / `v0.0.1-arm64` / `latest-arm64`
(main 分支推送不触发构建)。部署按机器架构拉对应标签,compose 默认 `latest-amd64`。

## 镜像内容

- 基础:`python:3.13-slim` + Node.js(sentinel 令牌的 `sentinel-runner.js` 需要)+ Chromium 系统依赖(apt)
- **浏览器二进制不进镜像**:首启时 entrypoint 检测 `/opt/ms-playwright` 卷,为空才下载
  playwright/patchright 的 chromium;cloakbrowser 二进制由库自身在首跑检测 `.cloakbrowser`
  卷缓存,没有才下载(需配置 `CLOAK_LICENSE_KEY`)
- 非 root 运行(uid 1000);运行态全部落卷,镜像无状态

## 卷一览

| 卷 | 容器路径 | 内容 |
|---|---|---|
| `app-state` | `/app/state` | `turb.sqlite3` 主库(镜像内已做符号链接) |
| `app-data` | `/app/data` | clash_nodes.json 等节点状态 |
| `app-logs` | `/app/logs` | WebUI 运行日志 |
| `app-reglogs` | `/app/注册日志` | 每任务注册日志 |
| `app-cloak` | `/home/app/.cloakbrowser` | cloakbrowser 浏览器二进制缓存 |
| `app-pw` | `/opt/ms-playwright` | playwright/patchright 浏览器二进制(首启下载) |
| bind | `/app/.env` | 配置(WebUI 配置页写回,**勿加 `:ro`**) |

## 部署步骤

1. **改 .env(容器关键项)**——容器里的 `127.0.0.1` 是容器自己,凡是要访问宿主机
   Clash 的地址一律改成 `host.docker.internal`:

   ```ini
   CLASH_PROXY_HOST="host.docker.internal"          # worker 端口 1100x 的主机名(新增配置项)
   CLASH_API_URL="http://host.docker.internal:9097" # external-controller
   PROXY_POOL="socks5://host.docker.internal:7897"  # 非自动选点路径的兜底代理
   ```

2. **宿主机 Clash 放行容器访问**(Verge):
   - 开启「允许 LAN」(7897 混合端口监听 0.0.0.0)
   - external-controller 改绑 `0.0.0.0:9097`(默认只听 127.0.0.1)
   - Merge.yaml 里的 worker listeners(11001-11003)同样要写 `0.0.0.0:1100x`

3. **启动**:

   ```bash
   docker compose up -d        # 本地无镜像时自动构建;也可 compose pull 拉 GHCR
   docker compose logs -f      # 首启会下载 playwright/cloak 浏览器二进制(落卷),耐心等
   # WebUI: http://localhost:5000
   ```

4. **更新版本**:

   ```bash
   docker compose pull && docker compose up -d
   ```

## 注意事项

- **国内网络构建**:apt 官方源(Fastly)常 502,本地构建切清华源——
  `APT_MIRROR=mirrors.tuna.tsinghua.edu.cn docker compose build`(GH Actions 用默认源即可)

- **数据备份** = 备份 `app-state` 卷(主库)+ `.env`。导出:`docker run --rm -v turb-gpt-free-register_app-state:/d alpine tar cz -C /d .`
- cloakbrowser 为商业库,容器内首跑需要有效的 `CLOAK_LICENSE_KEY` 且能出网
- 镜像按平台标签发布(`latest-amd64` / `latest-arm64` / `v*-amd64` / `v*-arm64`);arm64 走 QEMU 模拟构建较慢
- WebUI 是敏感控制台,`5000` 端口勿直接暴露公网;远程访问建议走 Tailscale/内网或加反代认证
- 镜像内不含 `.env` 与任何运行数据;`WEBUI_AUTH_CODE` 务必设置
