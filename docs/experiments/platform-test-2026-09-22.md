# Platform 授权实测报告 (2026-09-22)

## 结论摘要

| 问题 | 结果 |
|---|---|
| 注册流程能否跑通 + platform 授权? | **流程完全跑通**,但卡在邮箱资源 |
| 能否省下接码(免手机验证)? | **未能验证**(缺干净邮箱) |
| token 能否续期? | **未能验证**(需先拿到 token) |

## 一、已完成: 全链路流程验证 ✅

协议注册 + platform 驱动完整跑通(反复 5 次,每次走到同一位置):

```
首页预热(CF 质询可容忍) → 采纳服务端 oai-did → /auth/login_with 导航
→ providers(4个) → CSRF token → signin(screen_hint=signup)
→ authorize 重定向(偶发 SSLError,自动重试成功) → email-verification 页
→ Outlook Graph 取 OTP 成功(settle 5s 防旧码) → sentinel token 生成
  (turnstile=True, so=True, pow 全含) → 提交验证码
```

**唯一卡点**: OpenAI 返回 `account_deactivated`。

## 二、关键 Bug 修复: socks5 vs socks5h ⚠️

**症状**: `SSLError: TLS connect error: OPENSSL_internal:invalid library`

**根因**:
- `socks5://` → **本地** DNS 解析 → chatgpt.com 解析出 IPv6 `2001::c73b:95e7`
  → 本机无 IPv6 出口 → TLS 握手失败
- `socks5h://` → **代理端** DNS 解析 → 走 IPv4 → 正常

**实测对比**:
```
BrowserSession(proxy="socks5://127.0.0.1:7897")  → SSLError
BrowserSession(proxy="socks5h://127.0.0.1:7897") → 200/403 (正常)
```

**修复**: `.env` 的 `PROXY_POOL` 改为 `socks5h://127.0.0.1:7897`(已加注释)。
**注意**: 配置任何代理池都必须用 `socks5h://`,不要用 `socks5://`。

## 三、网络环境排障

### 免费节点池特性
- 300 节点 → L1 网络可达 147 → L2 三连测(chatgpt+auth+sentinel)通过 5 个
- **节点窗口以秒计**: 同一节点 10 秒前全通过,10 秒后全超时
- 有效策略: 探测到窗口后**立即**发起请求(见下方脚本模式)
- 可用节点参考: `极限白嫖https🇺🇸美国` / `9极限白嫖https🇺🇸美国` / `5极限白嫖https🇺🇸美国` 等

### 自建网关不可用
- `o8.t/resin.131518.xyz` 对 chatgpt.com 稳定 0.5s RST(换出口 IP/会话键/绕 DNS 均无效)
- 同网关访问 cloudflare/github/bing 正常 → **网关服务器侧对 chatgpt.com 出站故障**
- 不是出口 IP 被封(被封是 403 慢响应,不是秒级 RST)

## 四、阻塞原因: 测试资源彻底耗尽

| 资源 | 状态 |
|---|---|
| 邮箱池 50 个 | 36 个 used + 4 个 failed + **10 个 available 全部实测封禁** |
| 已注册账号 6 个 | access_token 全部失效(403 或 200 无 accessToken) |
| `用于注册的邮箱.txt` | **不存在**(可能已删除) |
| 其他数据源 | 无 |

**关键认知**: OpenAI 对**已封禁的邮箱永久拒绝重新注册**(账号级封禁,非限流)。
凡注册过且号被封的邮箱,都不能再用于测试。

## 五、续期验证: 初步实测结果 🔬

### 已测: 旧账号 RT 刷新(6 个)

用 6 个已注册账号的 refresh_token 直接调 `auth.openai.com/oauth/token`:

| client_id | 结果 |
|---|---|
| CLI `app_EMoamEEZ73f0CkXaXp7hrann` | 401 `token_expired` |
| platform `app_2SKx67EdpoN0G6j64rFvigXD` | 401 `token_expired` |

**关键发现 1(有价值)**: 两个 client_id **返回完全相同的错误**。
说明 `/oauth/token` 端点**接受两个 client_id 的请求格式**,错误来自 token 本身过期
(账号注册于 09-17,已 5 天),**不是 client 不匹配**。这排除了"端点拒绝 platform client"这个可能。

**关键发现 2(请求格式)**:
- `form-data` 格式 → 返回 HTML 页面(端点不接受)
- `JSON + Accept: application/json` → 返回正确的 JSON 错误响应 ✓
- `/api/accounts/oauth/token` (新版) → 400 `invalid_grant`

**结论**: client_id 兼容性风险**未排除但降低了**——端点不拒绝 platform client_id。
真正的答案(能否用 platform 的 RT 换到新 AT)仍需**新鲜 token** 才能确认。

### 待测: 新鲜 token 的续期

需要一次成功的 platform 授权产出 token,然后:
1. 立即用 platform client_id 刷新 → 预期成功
2. 用 CLI client_id 刷新同一 RT → 看是否失败(这才是 CPA 场景的风险点)
3. 若 CLI 刷新失败 → 证明 CPA 硬编码 client_id 会导致 platform 号无法续期

## 六、待验证(需要新资源)

### 1. 免接码验证
提供**从未注册过 OpenAI 的全新 outlook 邮箱** → 跑通注册 → 触发 platform 授权
→ 观察是否有 `/add-phone` 或 `/phone-verification` 跳转。
- 若全程无手机验证 → **免接码成立**,可省掉接码成本
- 若出现手机页 → 代码会抛 `_PHONE_REQUIRED_HINT`,需回退 protocol 接码驱动

### 2. 续期验证(生死点) ⚠️
**风险背景**: platform token 的 `id_token.aud` 是 `app_2SKx67EdpoN0G6j64rFvigXD`,
而 CPA 刷新时硬编码 CLI 的 `app_EMoamEEZ73f0CkXaXp7hrann`
(CLIProxyAPI PR #2153 提出该问题,当前 main 分支仍未修复)。

**验证方法**:
```bash
# 拿到 platform 授权产出的 refresh_token 后:
curl -X POST https://auth.openai.com/oauth/token   -H "Content-Type: application/json"   -d '{"client_id":"app_EMoamEEZ73f0CkXaXp7hrann","grant_type":"refresh_token","refresh_token":"<rt>"}'
# 再试 platform 的 client_id 对比
```
- **能刷新** → platform 方案可用,免接码 + 可续期
- **不能刷新** → platform 号在 AT 过期后集体失效,必须:
  a) 给 CPA 打 patch 支持 client_id 匹配,或
  b) 只用 protocol 驱动(带接码)

## 七、复现命令

```python
# 节点窗口探测 + 立即注册
import json, urllib.request, urllib.parse, time
from curl_cffi import requests as cr
BASE="http://127.0.0.1:9097"; HDR={"Authorization":"Bearer set-your-secret"}
def switch(n):
    enc=urllib.parse.quote("🚀 手动切换")
    urllib.request.urlopen(urllib.request.Request(f"{BASE}/proxies/{enc}",
        data=json.dumps({"name":n}).encode(),
        headers={**HDR,"Content-Type":"application/json"}, method="PUT"), timeout=10)
PROX={"http":"socks5h://127.0.0.1:7897","https":"socks5h://127.0.0.1:7897"}
# 探测窗口
for n in ['极限白嫖https🇺🇸美国','9极限白嫖https🇺🇸美国','5极限白嫖https🇺🇸美国']:
    switch(n); time.sleep(0.3)
    r = cr.get("https://chatgpt.com/", proxies=PROX, timeout=8, impersonate="chrome146", allow_redirects=False)
    if r.status_code == 200:
        print("窗口:", n); break
# 立即注册(用干净邮箱)
import main
r = main.run_registration(email="<干净邮箱>", name=None, birthday=None)
print(r)
```
