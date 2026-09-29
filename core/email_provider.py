# -*- coding: utf-8 -*-
"""
邮箱来源调度层。

精简后只保留 Outlook 邮箱池：
    EMAIL_SOURCE = "outlook"

历史上支持过的临时邮箱来源（generic_api / imap / cloudflare_domain /
cloudflare / gptmail / mailnest / cloudmail / remail）对应的客户端已删除；
旧 .env 里残留的来源名会被忽略并记一条 warning，最终按 outlook 处理。
"""
import logging
import time
from typing import Iterable

logger = logging.getLogger(__name__)

_VALID_SOURCES = ("outlook", "manager")


def parse_email_sources(value=None) -> list[str]:
    """把 EMAIL_SOURCE 解析为有序来源列表，去重并过滤空值。

    非 outlook 的历史来源会被忽略（对应客户端已下线），因此本函数现在
    正常只会返回 ["outlook"]。
    """
    if value is None:
        from config import email as _email_cfg
        value = _email_cfg.EMAIL_SOURCE
    if isinstance(value, str):
        raw = value.replace(";", ",").replace("|", ",").split(",")
    elif isinstance(value, Iterable):
        raw = list(value)
    else:
        raw = [value]

    out: list[str] = []
    for item in raw:
        s = str(item or "").strip().strip('"\'')
        if not s:
            continue
        if s not in _VALID_SOURCES:
            logger.warning(f"[EmailProvider] 邮箱来源 {s!r} 已下线（仅保留 outlook），已忽略")
            continue
        if s not in out:
            out.append(s)
    return out or ["outlook"]


def _pick_from_source(source: str) -> str:
    if source == "manager":
        return acquire_email_from_manager()
    from core.outlook_client import pick_account
    return pick_account().email


def acquire_email() -> str:
    """根据 EMAIL_SOURCE 领取一个用于注册的邮箱地址；多个来源时按顺序兜底。"""
    sources = parse_email_sources()
    last_exc: Exception | None = None
    for source in sources:
        try:
            email = _pick_from_source(source)
            logger.info(f"[EmailProvider] 使用邮箱来源: {source}, email={email}")
            return email
        except Exception as exc:
            last_exc = exc
            logger.warning(f"[EmailProvider] 来源 {source} 领取邮箱失败: {type(exc).__name__}: {exc}")
            continue
    raise RuntimeError(f"所有邮箱来源均领取失败: {sources}; last={last_exc}")


_MANAGER_CLAIMS: dict[str, dict] = {}


def acquire_email_from_manager() -> str:
    """从邮箱管理端项目领取一个 toClaim 邮箱(服务端自带领取锁)。"""
    import uuid
    from core.email_manager_client import get_client
    client = get_client()
    # 死号秒判: 领取后先看管理端记录的 token 刷新状态, 失败的直接
    # complete-failed 换下一个(否则要在注册流程里白等 90s×3 轮取码超时)
    for attempt in range(4):
        info = client.claim_email(caller_id="turb", task_id=uuid.uuid4().hex[:12])
        email = str(info.get("email") or "")
        _MANAGER_CLAIMS[email] = {
            "account_id": int(info.get("account_id") or 0),
            "claim_token": str(info.get("claim_token") or ""),
            "claimed_at": time.time(),
        }
        try:
            st, err = client.refresh_status_map().get(email.lower(), ("", ""))
        except Exception:
            st, err = "", ""
        if st != "failed":
            logger.info("[EmailProvider] 管理端领取: %s (account_id=%s)", email, info.get("account_id"))
            return email
        # 刷新状态 failed = token 已死, 秒标失败换下一个
        logger.warning("[EmailProvider] 领取到 token 已失效账号(%s), 秒标失败换下一个: %s", (err or st)[:80], email)
        manager_finish(email, success=False, transient=False, detail=f"管理端 token 刷新失败: {(err or st)[:120]}")
    raise LookupError("连续领取到多个 token 失效账号, 请在管理端处理失败邮箱后重试")


def manager_finish(email: str, *, success: bool, transient: bool = False, detail: str = "") -> bool:
    """把注册结果回写管理端: 成功→done; 临时失败→释放回池; 死号/封禁→failed。

    返回是否命中 manager 邮箱(本地池邮箱返回 False, 走原有 release 逻辑)。
    """
    claim = _MANAGER_CLAIMS.pop(email, None)
    if not claim:
        return False
    try:
        from core.email_manager_client import get_client
        client = get_client()
        # 标签同步: gpt可用 → gpt成功 / gpt失败(管理端前端按标签一目了然)
        _tags = {}
        try:
            _tags = {
                "ok": client.ensure_tag("gpt成功", "#3700ff"),
                "bad": client.ensure_tag("gpt失败", "#ff0000"),
                "ok_bad": client.ensure_tag("gpt可用", "#00bcf2"),
            }
        except Exception:
            _tags = {}
        if success:
            client.complete_success(claim["account_id"], claim["claim_token"], detail or "注册成功")
            logger.info("[EmailProvider] 管理端已标记成功: %s", email)
        elif transient:
            client.release(claim["account_id"], claim["claim_token"], detail or "临时失败回池")
            logger.info("[EmailProvider] 管理端已释放回池: %s", email)
        else:
            client.complete_failed(claim["account_id"], claim["claim_token"], detail or "注册失败")
            logger.warning("[EmailProvider] 管理端已标记失败: %s", email)
        try:
            if _tags.get("ok") and success:
                client.account_tag(claim["account_id"], _tags["ok"], "add")
                if _tags.get("ok_bad"):
                    client.account_tag(claim["account_id"], _tags["ok_bad"], "remove")
            elif _tags.get("bad") and not success and not transient:
                client.account_tag(claim["account_id"], _tags["bad"], "add")
                if _tags.get("ok_bad"):
                    client.account_tag(claim["account_id"], _tags["ok_bad"], "remove")
        except Exception:
            pass
    except Exception as exc:
        logger.warning("[EmailProvider] 管理端回写失败(租期到会自动释放): %s: %s", email, str(exc)[:120])
        _MANAGER_CLAIMS[email] = claim  # 回写失败放回, 避免泄漏; 租期兜底
        return True
    return True


def acquire_email_from_source(source: str) -> str:
    """从调用方指定的单一来源领取邮箱，不受 EMAIL_SOURCE 兜底顺序影响。"""
    source = str(source or "").strip().lower()
    if source not in _VALID_SOURCES:
        raise ValueError(f"不支持的邮箱来源: {source}")
    email = _pick_from_source(source)
    logger.info("[EmailProvider] 指定来源领取邮箱: source=%s, email=%s", source, email)
    return email


def acquire_email_after_input(email: str | None = None) -> str:
    """在浏览器已找到邮箱输入框后领取邮箱。

    浏览器驱动把“找到输入框”和“领取邮箱”拆成两个阶段，避免页面加载、风控
    或入口识别失败时提前消耗邮箱。传入已有邮箱时不重复领取，兼容固定邮箱模式。
    """
    current = str(email or "").strip()
    if current:
        return current

    from config import email as _email_cfg

    if not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", False)):
        raise RuntimeError("页面已找到邮箱输入框，但自动取邮箱未启用且未配置 REGISTER_EMAIL")
    allocated = str(acquire_email() or "").strip()
    if not allocated:
        raise RuntimeError("邮箱服务返回了空邮箱地址")
    logger.info("[EmailProvider] 已找到邮箱输入框，开始分配邮箱: %s", allocated)
    return allocated


def resolve_email_source(email: str) -> str:
    """根据邮箱判断实际来源，已注册账号优先使用落库来源。

    精简后只可能返回 "outlook"；落库来源若不是 outlook（历史账号）也会
    归一为 outlook，因为这些来源的取信客户端已删除。
    """
    # 已注册账号的 email_source 是注册时的最终来源。必须先读它，不能因为
    # 当前进程里恰好残留了其它邮箱池上下文，或邮箱池顺序发生变化，就把同一
    # 地址误判到另一个服务商。
    registered_source = _registered_email_source(email)
    if registered_source:
        return registered_source

    from core import db
    if db.get_outlook_by_email(email):
        return "outlook"
    return parse_email_sources()[0]


def _normalize_explicit_email_source(value: str | None) -> str | None:
    """规范化调用方明确指定的邮箱来源。

    已注册账号的 ``email_source`` 是注册时落库的单一来源，查活时应优先使用
    这个值，而不是重新根据当前进程的临时邮箱上下文或全局 EMAIL_SOURCE 猜测。
    历史数据里保存的已下线来源（imap/gptmail 等）返回 None，由调用方兜底。
    """
    if value is None:
        return None
    raw = str(value or "").strip()
    if not raw:
        return None
    for item in raw.replace(";", ",").replace("|", ",").split(","):
        source = str(item or "").strip().strip("\"'").lower()
        if source in _VALID_SOURCES:
            return source
    return None


def _registered_email_source(email: str) -> str | None:
    """读取已注册账号落库的邮箱来源。"""
    try:
        from core import db

        account = db.get_account_by_email(email)
    except Exception:
        return None
    return _normalize_explicit_email_source((account or {}).get("email_source"))


def wait_for_otp(
    email: str,
    after_ts: float,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
    email_source: str | None = None,
    force_service: bool = False,
) -> str:
    """等待并返回该邮箱最新的 ChatGPT OTP（6 位数字字符串）。

    USE_EMAIL_SERVICE=False 时走手动验证码通道（WebUI 提交 / CLI 输入），
    不再强制要求 Outlook clientId/refreshToken。
    """

    # manager 来源: 通过邮箱管理端读邮件(本项目不持有 refresh_token)
    if email in _MANAGER_CLAIMS:
        from config import email as _email_cfg
        from core.email_manager_client import get_client
        from core.outlook_client import looks_like_openai_email, extract_otp
        client = get_client()
        deadline_m = time.time() + (max_wait or _email_cfg.OTP_MAX_WAIT)
        best: str | None = None
        best_ts = 0.0
        last_err = ""
        while time.time() < deadline_m:
            try:
                items = client.fetch_emails(email)
                for item in items:
                    if not looks_like_openai_email(item):
                        continue
                    otp = extract_otp(item)
                    if not otp:
                        continue
                    ts = item.get("date") or ""
                    import datetime as _dt
                    try:
                        tts = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
                    except Exception:
                        tts = time.time()
                    if after_ts and tts < after_ts - 60:
                        continue
                    if tts > best_ts:
                        best_ts, best = tts, otp
                if best:
                    logger.info("[Outlook] (manager) 锁定 OTP=%s", best)
                    time.sleep(3)
                    return best
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {str(exc)[:100]}"
            time.sleep(_email_cfg.OTP_POLL_INTERVAL or 3)
        raise RuntimeError(f"(manager) 等待 {email} OTP 超时: {last_err or '未收到符合条件的邮件'}")
    try:
        from config import email as _email_cfg
        use_service = bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", True))
    except Exception:
        use_service = True

    if not use_service and not force_service:
        from core.manual_otp import wait_for_manual_otp
        from config import email as _email_cfg
        timeout = int(max_wait if max_wait is not None else (getattr(_email_cfg, "OTP_MAX_WAIT", 180) or 180))
        job_id = None
        try:
            from core import registration_service as svc
            job_id = getattr(svc._THREAD_CTX, "job_id", None)
        except Exception:
            job_id = None
        return wait_for_manual_otp(email, timeout=timeout, job_id=job_id)

    extra_kwargs = {}
    if max_wait is not None:
        extra_kwargs["max_wait"] = max_wait
    if poll_interval is not None:
        extra_kwargs["poll_interval"] = poll_interval
    if settle_seconds is not None:
        extra_kwargs["settle_seconds"] = settle_seconds

    # 精简后取信只剩 Outlook 一种实现：无论来源参数/落库来源是什么，
    # 统一走 Outlook client（远端 mail.chatai.codes / Graph 直连）。
    from core.outlook_client import fetch_latest_otp
    return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)


def email_material_line(email: str, source: str | None = None) -> str:
    """返回账号换绑后应保存的邮箱素材行。"""
    from core import db
    row = db.get_outlook_by_email(email)
    if row:
        return str(row.get("copy_line") or email)
    return str(email or "")


def release_email(email: str, status: str = "available", note: str | None = None) -> str:
    """按邮箱实际来源回收状态，返回来源名。"""
    if email in _MANAGER_CLAIMS:
        # 临时失败(available): 领取记录保留不弹 —— 同任务重试还要继续用这个
        # 邮箱取码; 弹掉会导致后续 OTP 走本地池老路径(未找到上下文)。
        # 若任务最终放弃, 领取租期(900s)到期后管理端自动释放, 不会泄漏。
        if status == "available":
            logger.info("[EmailProvider] 管理端邮箱临时失败,保留领取(租期兜底): %s", email)
            return "manager"
        manager_finish(
            email,
            success=False,
            transient=False,
            detail=note or "",
        )
        return "manager"
    source = resolve_email_source(email)
    from core.outlook_client import release_account
    release_account(email, status=status, note=note)
    return source


def release_email_if_unconsumed(email: str, note: str | None = None) -> bool:
    """回收仍停留在 used 的任务领取，且绝不覆盖已注册/已判废状态。"""
    if not (email or "").strip():
        return False

    from core import db

    changed = db.release_unconsumed_outlook(email, note=note)
    if changed:
        logger.info("[EmailProvider] 已回收未消耗邮箱: email=%s", email)
    return changed
