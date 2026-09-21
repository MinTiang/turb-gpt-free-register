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
from typing import Iterable

logger = logging.getLogger(__name__)

_VALID_SOURCES = ("outlook",)


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
