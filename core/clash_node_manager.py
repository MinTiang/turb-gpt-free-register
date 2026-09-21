# -*- coding: utf-8 -*-
"""Clash 节点管理核心:质量检测 / 状态持久化 / 冷却与限额 / 自动选点。

状态存 data/clash_nodes.json(与运行库同目录,人可读):
{ "<节点名>": {
    "exit_ip": str, "quality": "good|bad|unknown",
    "quality_detail": {...}, "last_check_at": ISO,
    "reg_count": int, "last_reg_at": ISO, "last_reg_email": str,
    "cooldown_until": ISO, "blocked": bool, "bad_until": ISO } }

选点规则(全部页面可配):
- 排除 blocked / bad 跳过窗口内 / 冷却中 / reg_count 达上限 的节点
- 质量结果过期(>TTL)或 unknown 的节点先复检,复检失败标 bad 并进入跳过窗口
- 优先级:reg_count 少的优先 → 最久未用的优先
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "clash_nodes.json"
_LOCK = threading.Lock()

_FAKE_MARKERS = ("剩余流量", "套餐到期", "重置", "官网", "流量", "到期", "过期", "群")

# 并发 worker 出口隔离:worker N 绑定分组「worker-N出口」+ 本地端口 1100N
# (由 Verge 全局 Merge.yaml 注入 listeners;不存在的 worker 回退主分组/主端口)
WORKER_GROUP_TEMPLATE = "worker-{n}出口"
WORKER_BASE_PORT = 11000


def worker_group(worker: int | None) -> str:
    if not worker or worker < 1:
        return _group()
    return WORKER_GROUP_TEMPLATE.format(n=worker)


def worker_proxy_port(worker: int | None) -> int:
    if not worker or worker < 1:
        return int(_cfg().CLASH_PROXY_PORT or 7897)
    return WORKER_BASE_PORT + int(worker)


def _worker_proxy(worker: int | None) -> dict:
    port = worker_proxy_port(worker)
    return {"http": f"http://127.0.0.1:{port}", "https": f"http://127.0.0.1:{port}"}


def _cfg():
    from config import clash_manager as cfg
    return cfg


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text: str) -> datetime | None:
    try:
        return datetime.strptime(str(text or ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _load_state() -> dict:
    try:
        if _STATE_PATH.exists():
            data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _save_state(state: dict) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def _node_state(state: dict, node: str) -> dict:
    return state.setdefault(node, {
        "exit_ip": "", "quality": "unknown", "quality_detail": {},
        "last_check_at": "", "reg_count": 0, "last_reg_at": "",
        "last_reg_email": "", "cooldown_until": "", "blocked": False,
    })


# ---------------- Clash API(读配置,兼容旧 clash_control 的常量默认) ----------------

def _api(path: str, method: str = "GET", body: dict | None = None, timeout: float = 6.0):
    import urllib.request
    cfg = _cfg()
    headers = {"Content-Type": "application/json"}
    secret = str(cfg.CLASH_API_SECRET or "").strip()
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    req = urllib.request.Request(
        f"{str(cfg.CLASH_API_URL).rstrip('/')}{path}",
        headers=headers, method=method,
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def _proxy() -> dict:
    port = int(_cfg().CLASH_PROXY_PORT or 7897)
    return {"http": f"http://127.0.0.1:{port}", "https": f"http://127.0.0.1:{port}"}


def _group() -> str:
    return str(_cfg().CLASH_SWITCH_GROUP or "🚀 手动切换")


def list_nodes() -> list[str]:
    """切换分组下的真实节点(排除流量信息伪装项)。"""
    import urllib.parse
    enc = urllib.parse.quote(_group())
    p = _api(f"/proxies/{enc}")
    all_nodes = p.get("all", []) or []
    return [n for n in all_nodes if n != "DIRECT" and not any(m in n for m in _FAKE_MARKERS)]


def current_node(worker: int | None = None) -> str:
    import urllib.parse
    enc = urllib.parse.quote(worker_group(worker))
    return str(_api(f"/proxies/{enc}").get("now") or "")


def switch_node(name: str, *, verify_ip: bool = False, worker: int | None = None) -> dict:
    """切换分组选中节点(worker 指定时切 worker 专属分组);verify_ip 取出口 IP。"""
    import urllib.parse
    import requests
    enc = urllib.parse.quote(worker_group(worker))
    try:
        _api(f"/proxies/{enc}", method="PUT", body={"name": name})
        # 出口 IP 已变:清空 BrowserSession 的地理缓存,让后续会话按新 IP
        # 重新生成语言/时区画像(否则沿用旧 IP 的 locale,指纹与 IP 矛盾)。
        from core.session import clear_geo_cache
        clear_geo_cache()
    except Exception as exc:
        return {"ok": False, "node": name, "error": f"切换失败: {exc}"}
    ip = ""
    if verify_ip:
        proxies = _worker_proxy(worker)
        for attempt in range(3):
            time.sleep(1.0 + attempt)
            try:
                r = requests.get("https://api.ipify.org", proxies=proxies, timeout=10)
                if r.status_code == 200 and r.text.strip():
                    ip = r.text.strip()
                    break
            except Exception:
                pass
        if not ip:
            return {"ok": False, "node": name, "error": "出口 IP 验证失败"}
    return {"ok": True, "node": name, "ip": ip}


# ---------------- 质量检测 ----------------

def quality_check_node(node: str, *, restore: str | None = None, worker: int | None = None) -> dict:
    """检测单节点质量:出口 IP → chatgpt.com 可达性 → auth.openai.com 可达性。

    判定 bad 的条件:出口拿不到 / chatgpt 403或超时 / auth 403或超时。
    restore 指定检测后恢复回的节点名(默认恢复原选中)。
    """
    import requests
    with _LOCK:
        prev = restore or current_node(worker)
    detail: dict = {}
    proxies = _worker_proxy(worker)
    sw = switch_node(node, verify_ip=True, worker=worker)
    if not sw.get("ok"):
        detail["error"] = sw.get("error") or "switch_failed"
        _record_quality(node, "bad", detail)
        if prev and prev != node:
            switch_node(prev)
        return {"node": node, "quality": "bad", **detail}
    ip = sw["ip"]
    detail["exit_ip"] = ip

    ok = True
    # 用注册同款 curl_cffi(TLS impersonate)探测——普通 requests 能过但
    # curl_cffi TLS 断的节点(免费订阅常见)必须在这一步拦下。
    from curl_cffi.requests import Session as CurlSession
    from config.browser import IMPERSONATE
    cs = CurlSession(impersonate=IMPERSONATE)
    cs.proxies = proxies
    cs.timeout = 8
    try:
        for label, url in (
            ("chatgpt", "https://chatgpt.com/"),
            ("auth", "https://auth.openai.com/"),
            ("sentinel", "https://sentinel.openai.com/backend-api/sentinel/frame.html"),
        ):
            try:
                r = cs.get(url, allow_redirects=False)
                detail[label] = r.status_code
                if r.status_code in (403, 407, 502, 503, 504) or r.status_code == 0:
                    ok = False
                    # 首个失败即判死:坏节点(尤其 CF 403)继续测剩余 URL 纯烧
                    # 时间——实测 403 后再测 auth/sentinel 白费 10-20s。
                    break
            except Exception as exc:
                detail[label] = f"err:{type(exc).__name__}"
                ok = False
                break
    finally:
        try:
            cs.close()
        except Exception:
            pass
    quality = "good" if ok else "bad"
    _record_quality(node, quality, detail)
    if not worker and prev and prev != node:
        # 主分组的临时探测要恢复;worker 分组保持选中(选点后直接用于注册)
        switch_node(prev)
    logger.info("[ClashMgr] 质量检测 %s → %s (%s)", node, quality, detail)
    return {"node": node, "quality": quality, **detail}


def _record_quality(node: str, quality: str, detail: dict) -> None:
    with _LOCK:
        state = _load_state()
        st = _node_state(state, node)
        st["quality"] = quality
        st["quality_detail"] = detail
        if detail.get("exit_ip"):
            st["exit_ip"] = detail["exit_ip"]
        st["last_check_at"] = _iso(_now())
        # bad 跳过窗口:窗口内 pick_candidate_nodes 直接排除,避免每个任务
        # 对同一批坏节点重复串行探测(2026-09-20 实测一轮全扫要烧几分钟)。
        skip_h = float(getattr(_cfg(), "CLASH_BAD_NODE_SKIP_HOURS", 0) or 0)
        if quality == "bad" and skip_h > 0:
            st["bad_until"] = _iso(_now() + timedelta(hours=skip_h))
        else:
            st["bad_until"] = ""
        _save_state(state)


def _quality_fresh(st: dict) -> bool:
    cfg = _cfg()
    checked = _parse_iso(st.get("last_check_at") or "")
    if not checked:
        return False
    ttl_h = float(cfg.CLASH_QUALITY_TTL_HOURS or 6)
    return (_now() - checked) < timedelta(hours=ttl_h)


# ---------------- 限额与冷却 ----------------

def _ip_summary(state: dict) -> dict:
    s = state.get("_ip_summary")
    return s if isinstance(s, dict) else {}


def _available(st: dict, cfg, ip_summary: dict | None = None) -> tuple[bool, str]:
    if st.get("blocked"):
        return False, "blocked"
    max_n = int(cfg.CLASH_MAX_ACCOUNTS_PER_NODE or 2)
    if int(st.get("reg_count") or 0) >= max_n:
        return False, "max_accounts"
    until = _parse_iso(st.get("cooldown_until") or "")
    if until and _now() < until:
        return False, "cooldown"
    # IP 级聚合:vless/trojan 同名节点共享出口 IP,限额/冷却按 IP 合并计算
    ip = str(st.get("exit_ip") or "")
    if ip and isinstance(ip_summary, dict):
        ipst = ip_summary.get(ip) or {}
        if int(ipst.get("reg_count") or 0) >= max_n:
            return False, "max_accounts(ip)"
        ip_until = _parse_iso(ipst.get("cooldown_until") or "")
        if ip_until and _now() < ip_until:
            return False, "cooldown(ip)"
    return True, ""


_DELAY_CACHE: dict = {"at": 0.0, "map": {}}
_DELAY_LOCK = threading.Lock()


def _run_group_delay_once(timeout_ms: int) -> dict[str, int]:
    import urllib.parse
    enc = urllib.parse.quote(_group())
    url = f"/group/{enc}/delay?url=https%3A%2F%2Fchatgpt.com%2F&timeout={timeout_ms}"
    result = _api(url, timeout=timeout_ms / 1000 + 15)
    return {k: v for k, v in (result or {}).items() if isinstance(v, int)}


def group_delay_filter(*, timeout_ms: int = 8000, ttl_seconds: float = 600,
                       min_sane: int = 20) -> dict[str, int]:
    """L1 粗筛:mihomo 分组延迟 API 并行测全池(~8s),返回 {节点: 延迟ms}。

    只筛网络可达性(403 的节点也有延迟),CF 封锁靠 L2 curl_cffi 检测。
    单飞锁:并发任务同时触发时只发一次 L1 请求——mihomo 同时处理多个全组
    延迟测试会返回残缺结果(实测只剩 1-2 个节点),一旦缓存会拖垮所有选点。
    结果小于 min_sane 视为异常,立即重测一次。
    """
    with _DELAY_LOCK:
        if time.time() - _DELAY_CACHE["at"] < ttl_seconds and len(_DELAY_CACHE["map"]) >= min_sane:
            return _DELAY_CACHE["map"]
        alive = {}
        try:
            alive = _run_group_delay_once(timeout_ms)
        except Exception as exc:
            logger.warning("[ClashMgr] 分组延迟测试失败(跳过L1): %s", exc)
            return {}
        if len(alive) < min_sane:
            # 残缺结果(并发竞争/上游抖动):清缓存重测一次
            logger.warning("[ClashMgr] L1 结果异常偏小(%d 个),重测一次", len(alive))
            try:
                time.sleep(1.0)
                alive = _run_group_delay_once(timeout_ms)
            except Exception as exc:
                logger.warning("[ClashMgr] L1 重测失败(跳过L1): %s", exc)
                return {}
        _DELAY_CACHE["at"] = time.time()
        _DELAY_CACHE["map"] = alive
        logger.info("[ClashMgr] L1 粗筛: %d 个节点网络可达", len(alive))
        return alive


def pick_candidate_nodes() -> list[tuple[str, str, dict]]:
    """按优先级返回可用候选 [(node, reason_empty, state)]。blocked/超限/冷却的排除。"""
    cfg = _cfg()
    with _LOCK:
        state = _load_state()
    nodes = list_nodes()
    ip_summary = _ip_summary(state)
    alive = group_delay_filter()
    candidates = []
    for n in nodes:
        if alive and n not in alive:
            continue  # L1: 网络不可达,直接排除
        st = state.get(n) or {}
        # bad 跳过窗口:窗口内的节点直接排除,不再复测(CF 封禁通常持续数小时,
        # 复测纯烧时间)。窗口过期后节点重新参与候选并复检。
        bad_until = _parse_iso(st.get("bad_until") or "")
        if bad_until and bad_until > _now():
            continue
        ok, _reason = _available(st, cfg, ip_summary)
        if ok:
            candidates.append((n, st))
    # reg_count 升序 → 延迟低优先(L1 有数据时)
    def _last_reg(st):
        return _parse_iso(st.get("last_reg_at") or "") or datetime(1970, 1, 1, tzinfo=timezone.utc)
    candidates.sort(key=lambda x: (int(x[1].get("reg_count") or 0), alive.get(x[0], 99999)))
    return [(n, "", st) for n, st in candidates]


def acquire_node_for_registration(*, max_attempts: int = 20, worker: int | None = None) -> dict:
    """为一次注册挑选并切换到合格节点。

    worker=N 时操作 worker-N 专属分组与端口(1100N),与其他 worker/主出口
    完全隔离;返回 {ok, node, ip, proxy_port}。
    """
    cfg = _cfg()
    tried = 0
    last_reason = ""
    for node, _r, st in pick_candidate_nodes():
        if tried >= max_attempts:
            break
        tried += 1
        if not _claim_node(node):
            continue  # 其他 worker 正在检测/使用该节点(动态分牌)
        try:
            if bool(cfg.CLASH_QUALITY_CHECK_ENABLED):
                need_check = st.get("quality") != "good" or not _quality_fresh(st)
                if need_check:
                    result = quality_check_node(node, worker=worker)
                    if result.get("quality") != "good":
                        last_reason = f"{node}: 质量不佳({result.get('error') or result.get('chatgpt') or result.get('auth')})"
                        release_node_claim(node, result.get("exit_ip") or "")
                        continue
            sw = switch_node(node, verify_ip=True, worker=worker)
            if not sw.get("ok"):
                _record_quality(node, "bad", {"error": sw.get("error")})
                last_reason = f"{node}: {sw.get('error')}"
                release_node_claim(node)
                continue
            ip = str(sw.get("ip") or "")
            if not _claim_ip(ip):
                # 节点名不同但出口 IP 相同(vless/trojan 对)且已被占用
                last_reason = f"{node}: 出口IP {ip} 已被其他 worker 占用"
                release_node_claim(node)
                continue
            port = worker_proxy_port(worker)
            logger.info("[ClashMgr] 注册选用节点 %s (ip=%s, worker=%s, port=%s)", node, ip, worker or "main", port)
            # 占用保持到 record_registration(注册全程独占)
            return {"ok": True, "node": node, "ip": ip, "proxy_port": port}
        except Exception:
            release_node_claim(node, str((st or {}).get("exit_ip") or ""))
            raise
    return {"ok": False, "error": f"无可用节点(tried={tried}; {last_reason})"}


def mark_node_bad_from_registration(node: str, detail: dict | None = None) -> None:
    """注册中途判定节点不可用(403/会话熔断等):标 bad 进跳过窗口。

    质量探测通过但注册中途被 CF 拦,说明该 IP 段当前实际被标记;
    不进跳过窗口的话下一个任务还会选中它再失败一次。
    """
    try:
        if node:
            _record_quality(node, "bad", {"error": "registration_failed", **(detail or {})})
    except Exception:
        logger.exception("[ClashMgr] 标记节点 bad 失败: %s", node)


def record_registration(node: str, email: str, *, success: bool) -> None:
    """注册结束后记账:成功才计数+进入冷却;失败也记录尝试。"""
    cfg = _cfg()
    with _LOCK:
        state = _load_state()
        st = _node_state(state, node)
        st["last_reg_email"] = email
        st["last_reg_at"] = _iso(_now())
        release_node_claim(node, str((state.get(node) or {}).get("exit_ip") or ""))
        ip_summary = _ip_summary(state)
        cd_h = float(cfg.CLASH_NODE_COOLDOWN_HOURS or 12)
        st["fail_count"] = int(st.get("fail_count") or 0) + (0 if success else 1)
        st["cooldown_until"] = _iso(_now() + timedelta(hours=cd_h))
        if success:
            st["reg_count"] = int(st.get("reg_count") or 0) + 1
        # 成功与失败都做 IP 级冷却:失败的典型场景是 OpenAI 对烧过 IP 静默不发
        # OTP(根路径 200 检测不到),不隔离会连续烧邮箱。
        ip = str(st.get("exit_ip") or "")
        if ip:
            ipst = ip_summary.setdefault(ip, {"reg_count": 0, "cooldown_until": ""})
            if success:
                ipst["reg_count"] = int(ipst.get("reg_count") or 0) + 1
            ipst["cooldown_until"] = st["cooldown_until"]
            state["_ip_summary"] = ip_summary
        _save_state(state)
    logger.info("[ClashMgr] 节点记账 %s success=%s email=%s count=%s",
                node, success, email, st.get("reg_count"))


# ---------------- 节点占用声明(并发 worker 动态分牌) ----------------
# worker 选点/注册期间独占节点与其出口 IP:其他 worker 的候选迭代直接跳过,
# 避免并发 worker 按同一顺序撞同一个节点(或 vless/trojan 同 IP 对)。
# record_registration / release_node_claim 释放;进程内有效。
_NODE_CLAIM_LOCK = threading.Lock()
_claimed_nodes: set[str] = set()
_claimed_ips: set[str] = set()


def _claim_node(node: str) -> bool:
    with _NODE_CLAIM_LOCK:
        if node in _claimed_nodes:
            return False
        _claimed_nodes.add(node)
        return True


def _claim_ip(ip: str) -> bool:
    if not ip:
        return True
    with _NODE_CLAIM_LOCK:
        if ip in _claimed_ips:
            return False
        _claimed_ips.add(ip)
        return True


def release_node_claim(node: str, exit_ip: str = "") -> None:
    """释放节点与出口 IP 占用(未占用时无操作;注册结束/异常路径都应调用)。"""
    with _NODE_CLAIM_LOCK:
        _claimed_nodes.discard(node)
        if exit_ip:
            _claimed_ips.discard(exit_ip)


def claimed_snapshot() -> dict:
    with _NODE_CLAIM_LOCK:
        return {"nodes": sorted(_claimed_nodes), "ips": sorted(_claimed_ips)}


# ---------------- worker 槽位(WebUI 并发任务用) ----------------
_WORKER_SLOTS = (1, 2, 3)
_worker_slot_lock = threading.Lock()
_worker_slots_in_use: set[int] = set()


def acquire_worker_slot(timeout: float = 120.0) -> int | None:
    """取一个空闲 worker 槽位(1-3);全忙时最多等 timeout 秒,失败返回 None。

    槽位对应 mihomo listeners 的 11001-11003;WebUI 并发任务各占一槽,
    出口互不干扰。槽位在任务结束(成功/失败/停止)后必须归还。
    """
    deadline = time.time() + max(1.0, timeout)
    while time.time() < deadline:
        with _worker_slot_lock:
            free = [w for w in _WORKER_SLOTS if w not in _worker_slots_in_use]
            if free:
                slot = free[0]
                _worker_slots_in_use.add(slot)
                return slot
        time.sleep(1.0)
    return None


def release_worker_slot(worker: int) -> None:
    with _worker_slot_lock:
        _worker_slots_in_use.discard(worker)


# ---------------- 管理操作(页面用) ----------------

def nodes_overview() -> list[dict]:
    """页面展示:全部节点 + 状态 + 可用性。"""
    cfg = _cfg()
    with _LOCK:
        state = _load_state()
    out = []
    current = current_node()
    ip_summary = _ip_summary(state)
    for n in list_nodes():
        st = dict(state.get(n) or {})
        ok, reason = _available(st, cfg, ip_summary)
        cd_until = _parse_iso(st.get("cooldown_until") or "")
        remain_h = round((cd_until - _now()).total_seconds() / 3600, 1) if cd_until and cd_until > _now() else 0
        bad_until = _parse_iso(st.get("bad_until") or "")
        bad_remain_h = round((bad_until - _now()).total_seconds() / 3600, 1) if bad_until and bad_until > _now() else 0
        out.append({
            "node": n,
            "current": n == current,
            "exit_ip": st.get("exit_ip") or "",
            "quality": st.get("quality") or "unknown",
            "quality_detail": st.get("quality_detail") or {},
            "last_check_at": st.get("last_check_at") or "",
            "reg_count": int(st.get("reg_count") or 0),
            "last_reg_email": st.get("last_reg_email") or "",
            "cooldown_remain_h": max(0.0, remain_h),
            "bad_skip_remain_h": max(0.0, bad_remain_h),
            "available": ok,
            "unavailable_reason": reason,
        })
    return out


def reset_node(node: str | None = None, *, mode: str = "all") -> dict:
    """重置节点状态。node=None 表示全部;mode: cooldown/count/quality/all。"""
    with _LOCK:
        state = _load_state()
        targets = [node] if node else list(state.keys())
        changed = 0
        for n in targets:
            if n not in state:
                continue
            st = state[n]
            if mode in ("cooldown", "all"):
                st["cooldown_until"] = ""
            if mode in ("count", "all"):
                st["reg_count"] = 0
                st["last_reg_email"] = ""
                st["last_reg_at"] = ""
            if mode in ("quality", "all"):
                st["quality"] = "unknown"
                st["last_check_at"] = ""
                st["bad_until"] = ""
            if mode == "all":
                st["blocked"] = False
            changed += 1
        _save_state(state)
    return {"ok": True, "changed": changed, "mode": mode, "node": node or "all"}


def check_all_nodes() -> list[dict]:
    """顺序检测全部节点质量(页面按钮)。"""
    results = []
    prev = current_node()
    for n in list_nodes():
        results.append(quality_check_node(n, restore=prev))
    if prev:
        switch_node(prev)
    return results
