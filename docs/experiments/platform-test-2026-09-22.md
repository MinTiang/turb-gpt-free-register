# Platform 授权实测最终报告 (2026-09-22)

## 🎯 核心结论: 三个问题全部有答案

| 问题 | 答案 | 证据 |
|---|---|---|
| **能不能免接码?** | ✅ **成立** | 全程仅邮箱 OTP,零手机验证页 |
| **能不能续期?** | ✅ **可以**(用 platform client_id) | 连续 3 次刷新成功,RT 持续轮换 |
| **CPA 能用吗?** | ❌ **不能**(当前版本) | CPA 硬编码 CLI client_id → 401 |

**一句话总结**: platform 授权技术上完全可行(免接码 + 可续期),但**必须修复 CPA 的 client_id 硬编码问题**,否则 token 过期后无法自动刷新。

---

## 一、免接码验证 ✅

完整授权日志(账号 `uzgahdgx70791@outlook.com`):

```
[Codex][Platform] 开始免接码授权（复用注册登录态）
[Codex][Platform] authorize 落点: https://auth.openai.com/log-in/password
[Codex][Platform] 已提交邮箱，进入登录验证
[Codex][Platform] 登录验证码已发送
[Codex][Platform] 邮箱 OTP 收到：230415
[Codex][Platform] 邮箱 OTP 验证通过              ← 无手机验证!
[Codex][Platform] 已拿到 authorization code：ac_-_SPJNSDASsPW...
[Codex][Platform] 新版 token 端点换取成功
[Codex][Platform] CPA auth-file 上传成功
[Codex][Platform] 成功：uzgahdgx70791@outlook.com
status: success
```

**关键**: 全程**零手机验证**。对比 protocol 驱动(必过 `add-phone/send` + 短信接码),
platform 驱动走 `auth.openai.com/log-in/password` → 邮箱 OTP → 直接拿 code。

---

## 二、续期验证 ✅ (核心发现)

### Token 归属确认

```
access_token.client_id = app_2SKx67EdpoN0G6j64rFvigXD   ← platform
id_token.aud          = app_2SKx67EdpoN0G6j64rFvigXD
```

### 刷新测试矩阵

| client_id | 结果 |
|---|---|
| `app_2SKx67EdpoN0G6j64rFvigXD` (platform,签发者) | ✅ **200 成功**,返回新 AT + 轮换的新 RT |
| `app_EMoamEEZ73f0CkXaXp7hrann` (CLI,CPA 硬编码) | ❌ **401 `invalid_client`: Invalid client specified** |

**连续 3 次刷新均成功**,RT 每次都轮换(new refresh_token),证明 platform token 可持续续期。

---

## 三、CPA 集成问题 ❌ (必须修复)

### 实测证据

调用 CPA 管理接口触发刷新:
```bash
POST /v0/management/auth-files/refresh
→ HTTP 500
{"error":"token refresh failed after 3 attempts: token refresh failed with status 401:
  {\"error\": {\"message\": \"Invalid client specified.\", \"code\": \"invalid_client\"}}"}
```

### 根因

CLIProxyAPI `openai_auth.go:213-218` 的 `refreshTokensSingleFlight` **硬编码** `client_id`:
```go
"client_id": {ClientID},  // = app_EMoamEEZ73f0CkXaXp7hrann
```
而 platform token 需要 `app_2SKx67EdpoN0G6j64rFvigXD`。[CLIProxyAPI PR #2153](https://github.com/router-for-me/CLIProxyAPI/pull/2153)
提出过该问题(**未合并**,当前 main 分支仍未修复)。

### 影响

- platform 号的 auth-file 上传后**能立即使用**(AT 有效期内)
- **AT 过期后 CPA 无法刷新** → 账号在 CPA 里失效
- AT 有效期实测: 从 JWT `exp` 看约 10 天(1790011895 → 1790875895)

### 修复方案(三选一)

1. **给 CPA 打 patch**(推荐): 刷新时从 auth-file 读取 client_id,而非硬编码。
   auth-file 里已有线索: `access_token.client_id` 字段。
2. **CPA 支持 auth-file 记录 client_id**: 在 codex-*.json 里加 `client_id` 字段,
   CPA 刷新时优先用它。需 CPA 侧支持。
3. **自建刷新服务**: turb 侧定时用 platform client_id 刷新,写回 CPA。
   (项目已有 `codex_retry_service` 可挂载)

---

## 四、本次修复的 Bug

### 1. socks5 vs socks5h (影响所有注册)
`socks5://` 本地 DNS 解析出 IPv6 → TLS 失败。必须用 `socks5h://`。已修 `.env`。

### 2. CPA auth-file 上传 (阻塞 platform 授权)
`core/codex_platform_oauth.py:_upload_cpa_auth_file` 用 curl_cffi 的 `files=` 参数,
但 curl_cffi 不支持(`NotImplementedError: files is not supported, use multipart`)。
**已修复**: 改用 `requests`(管理接口无需 TLS 指纹伪装),与 `codex_oauth.py` 一致。

### 3. 恢复 Cloudflare 临时邮箱配置
精简时删掉了 `config/email.py` 的 CLOUDFLARE_* 常量,导致该邮箱源不可用。
**已恢复**(服务 `mail.131518.xyz` 仍在运行,可无限建临时邮箱)。

---

## 五、发现的资源问题

**邮箱池状态**: 50 个邮箱中,
- 36 个 `used`(注册过,账号多已封禁)
- 4 个 `failed`(确认封禁)
- 10 个 `available`(实测部分仍可注册!)

**重要**: 本次测试用 `uzgahdgx70791@outlook.com`(标注 available)**注册成功**,
说明"available 全封"的早期判断有误——**available 里仍有可用邮箱**。

**OpenAI 封禁规则**: 已封禁账号的邮箱**永久拒绝重新注册**(`account_deactivated`),
但**从未注册过的邮箱可以正常注册**。

---

## 六、复现命令

```python
# 1. 全新邮箱注册 + platform 授权(一条链路)
import main
r = main.run_registration(email="<新邮箱>", name=None, birthday=None)
# → 注册 + 自动 platform 授权 + 上传 CPA

# 2. 单独测 platform 授权(已有账号)
from core.codex_oauth import run_codex_oauth
r = run_codex_oauth("<邮箱>", force=True)

# 3. 续期测试
from curl_cffi import requests as cr
r = cr.post("https://auth.openai.com/oauth/token",
    json={"client_id": "app_2SKx67EdpoN0G6j64rFvigXD", "grant_type": "refresh_token",
          "refresh_token": "<RT>"},
    headers={"Content-Type": "application/json", "Accept": "application/json"},
    proxies={"http":"socks5h://127.0.0.1:7897","https":"socks5h://127.0.0.1:7897"},
    timeout=25, impersonate="chrome146")
```
