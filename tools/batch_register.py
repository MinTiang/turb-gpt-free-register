# -*- coding: utf-8 -*-
"""批量协议注册器(exp-007 配方)。

用法:
  python tools/batch_register.py --count 10 --prefix batch1 [--spacing 360]

流程(每个账号):
  1. 领邮箱 → 协议+无密码注册(环境变量注入,不动 .env)
  2. 注册成功 → 立即轻预热(conversation/init + announcement 等使用信号)
  3. 间隔 spacing 秒后下一个
失败处理:网络类失败换代理池出口重试同一邮箱(最多2次);邮箱类失败(OTP超时等)
标记邮箱并领新邮箱。记录写 docs/experiments/protocol_experiments.jsonl。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

EXP_FILE = PROJECT_ROOT / "docs" / "experiments" / "protocol_experiments.jsonl"
EXP_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("batch")

_NAMES = ["Alex Turner", "Maria Lopez", "James Carter", "Emma Wilson", "David Chen",
          "Sophia Martin", "Daniel Scott", "Olivia Adams", "Liam Turner", "Grace Hall",
          "Ethan Brooks", "Chloe Bennett"]


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _save_row(row: dict) -> None:
    with EXP_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _apply_recipe_env(driver: str = "protocol", password: bool = False) -> None:
    """注入本进程配方(不动 .env)。

    protocol: exp-007 配方(无密码——user/register 是协议号强标记信号)
    cloak:    真实浏览器执行 user/register 合法,默认带密码(存活样本均有密码)
    """
    os.environ["REGISTRATION_DRIVER"] = driver
    os.environ["REGISTER_WITH_PASSWORD"] = "True" if password else "False"
    os.environ["TELEMETRY_ENABLED"] = "False"
    os.environ["CHATGPT_ANON_BOOTSTRAP_ENABLED"] = "True"
    import importlib
    for mod in ("config.env_loader", "config.register", "config.email"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
        else:
            importlib.import_module(mod)


def _looks_network_error(text: str) -> bool:
    """是否应换出口重试:网络类错误 + OTP 超时(典型为烧过 IP 被静默不发码)。"""
    low = str(text).lower()
    return any(k in low for k in (
        "403", "timeout", "timed out", "curl", "proxy", "connection",
        "reset", "ssl", "熔断", "tempfail", "临时失败",
        "otp 超时", "验证码超时", "等待", "90s"))


def register_one(exp_id: str, email: str) -> dict:
    """执行单账号注册+预热,返回实验行。代理由 PROXY_POOL 决定。"""
    from main import run_registration
    row = {
        "id": exp_id, "ts": _utcnow(), "email": email,
        "vars": {"driver": "protocol", "password": False, "warmup_messages": "light",
                 "dwell": "default", "anon_bootstrap": True, "telemetry": False,
                 "batch": True},
        "checks": [],
    }
    reg = None
    try:
        reg = run_registration(email=email, name=random.choice(_NAMES))
        row["register_result"] = {"success": bool(reg.get("success")),
                                  "error": reg.get("error"),
                                  "account_id": reg.get("account_id")}
    except Exception as exc:
        row["register_result"] = {"success": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    # 注册成功 → 轻预热(决定性生存因子,exp-012 对照确证)
    if reg and reg.get("success"):
        at = str(reg.get("access_token") or "")
        if at:
            try:
                from core.conversation_warmup import warmup_account
                warm = warmup_account(email, at, messages=1)
                row["warmup_results"] = warm
            except Exception as exc:
                row["warmup_results"] = [{"ok": False, "status": f"exc:{type(exc).__name__}"}]
    _save_row(row)
    return row


def _run_one_account(exp_id: str, worker: int, args) -> bool:
    """单个账号的注册+重试流程。返回是否成功。"""
    from core.outlook_client import pick_account

    acct = pick_account()
    email = acct.email
    logger.info("[%s] 邮箱: %s (worker%s)", exp_id, email, worker)

    row = register_one(exp_id, email)
    success = bool(row["register_result"].get("success"))

    retries = 0
    while (not success) and retries < args.max_retries and _looks_network_error(
            str(row["register_result"].get("error") or "")):
        retries += 1
        logger.warning("[%s] 网络类失败,换出口重试 %d/%d: %s", exp_id, retries,
                       args.max_retries, str(row["register_result"].get("error"))[:120])
        time.sleep(20)
        row = register_one(exp_id + "r%d" % retries, email)
        success = bool(row["register_result"].get("success"))

    if success:
        logger.info("[%s] ✓ 注册成功: %s", exp_id, email)
    else:
        logger.info("[%s] ✗ 注册失败: %s", exp_id, str(row["register_result"].get("error"))[:160])
    return success


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--prefix", default="batch1")
    ap.add_argument("--spacing", type=int, default=150, help="同 worker 相邻注册间隔秒")
    ap.add_argument("--workers", type=int, default=1, help="并发 worker 数(最多3)")
    ap.add_argument("--driver", default="protocol", choices=["protocol", "cloak"],
                    help="注册驱动: protocol=纯协议(exp-007配方); cloak=Cloak浏览器(默认带密码)")
    ap.add_argument("--password", action="store_true", default=None,
                    help="强制带密码注册(不传则按驱动默认: protocol=无, cloak=有)")
    ap.add_argument("--max-retries", type=int, default=2, help="单账号换出口重试次数")
    args = ap.parse_args()
    args.workers = max(1, min(args.workers, 3))
    if args.password is None:
        args.password = args.driver == "cloak"

    _apply_recipe_env(driver=args.driver, password=args.password)
    logger.info("[batch] 配方已注入: driver=%s password=%s 关遥测; workers=%d",
                args.driver, args.password, args.workers)

    ok_count = 0
    fail_count = 0
    if args.workers == 1:
        for i in range(1, args.count + 1):
            exp_id = "%s-%02d" % (args.prefix, i)
            logger.info("========== %s (%d/%d) ==========", exp_id, i, args.count)
            if _run_one_account(exp_id, worker=1, args=args):
                ok_count += 1
            else:
                fail_count += 1
            if i < args.count:
                wait = args.spacing + random.randint(0, 60)
                logger.info("等待 %ds 后继续…", wait)
                time.sleep(wait)
    else:
        import threading

        def _worker_loop(worker: int, counter: dict, lock: threading.Lock()):
            while True:
                with lock:
                    if counter["done"] >= args.count:
                        return
                    counter["done"] += 1
                    seq = counter["done"]
                exp_id = "%s-%02d" % (args.prefix, seq)
                logger.info("========== %s (worker%d, %d/%d) ==========",
                            exp_id, worker, seq, args.count)
                try:
                    ok = _run_one_account(exp_id, worker=worker, args=args)
                except Exception as exc:
                    logger.error("[%s] worker 异常: %s", exp_id, exc)
                    ok = False
                with lock:
                    if ok:
                        counter["ok"] += 1
                    else:
                        counter["fail"] += 1
                time.sleep(args.spacing + random.randint(0, 60))

        counter = {"done": 0, "ok": 0, "fail": 0}
        lock = threading.Lock()
        threads = [threading.Thread(target=_worker_loop, args=(w, counter, lock),
                                    name="reg-worker-%d" % w, daemon=True)
                   for w in range(1, args.workers + 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ok_count = counter["ok"]
        fail_count = counter["fail"]

    logger.info("========== 批量完成: 成功 %d / 失败 %d ==========", ok_count, fail_count)
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
