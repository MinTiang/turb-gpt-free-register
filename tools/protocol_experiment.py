# -*- coding: utf-8 -*-
"""协议注册受控实验编排器。

用法:
  python tools/protocol_experiment.py run --id exp-001 --node "[vless]美国-普通-1-v" \
      [--no-password] [--warmup 1] [--dwell 18,45] [--no-bootstrap] [--spacing 600]
  python tools/protocol_experiment.py check          # 存活巡检(便宜: 只查 AT)
  python tools/protocol_experiment.py status         # 汇总表

实验记录: docs/experiments/protocol_experiments.jsonl(逐行 JSON,append-only)
原则: 每次实验独立节点;邮箱按需领取;全部变量落记录。
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("experiment")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_all() -> list[dict]:
    if not EXP_FILE.exists():
        return []
    rows = []
    for line in EXP_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def _save_row(row: dict) -> None:
    with EXP_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _update_row(exp_id: str, mutator) -> None:
    rows = _load_all()
    for i, r in enumerate(rows):
        if r.get("id") == exp_id:
            mutator(r)
            rows[i] = r
            break
    EXP_FILE.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


def cmd_run(args) -> int:
    from core import clash_control

    # 1. 切换节点并验证出口
    switch = clash_control.switch_node(args.node)
    if not switch.get("ok"):
        logger.error("节点切换失败: %s", switch)
        return 2
    exit_ip = switch["ip"]
    logger.info("[Exp %s] 节点就绪: %s ip=%s", args.id, args.node, exit_ip)

    # 2. 变量注入(必须在 import config 之前改 os.environ,配合 reload)
    os.environ["REGISTRATION_DRIVER"] = "protocol"  # 实验对象固定为协议驱动
    os.environ["REGISTER_WITH_PASSWORD"] = "False" if args.no_password else "True"
    if args.dwell:
        os.environ["POST_REGISTER_DWELL_SECONDS_RANGE"] = args.dwell
    os.environ["CHATGPT_ANON_BOOTSTRAP_ENABLED"] = "False" if args.no_bootstrap else "True"
    os.environ["TELEMETRY_ENABLED"] = "False"  # 已证伪的变量固定关闭

    # 注意: 不能用 config.reload_all()——它内部 load_env(override=True) 会把
    # .env 文件值重新灌回 os.environ,覆盖上面的实验变量。直接重载目标模块,
    # 它们的 apply_env_overrides 读的是当前 os.environ。
    import importlib
    for _mod in ("config.env_loader", "config.register", "config.roxybrowser",
                 "config.openai_protocol", "config.email"):
        if _mod in sys.modules:
            importlib.reload(sys.modules[_mod])
        else:
            importlib.import_module(_mod)

    # 3. 领邮箱 + 注册
    from core.outlook_client import pick_account, release_account
    acct = pick_account()
    email = acct.email
    logger.info("[Exp %s] 领取邮箱: %s", args.id, email)

    from main import run_registration
    row = {
        "id": args.id,
        "ts": _utcnow(),
        "email": email,
        "vars": {
            "driver": "protocol",
            "password": not args.no_password,
            "warmup_messages": args.warmup,
            "dwell": args.dwell or "default",
            "anon_bootstrap": not args.no_bootstrap,
            "telemetry": False,
        },
        "node": args.node,
        "exit_ip": exit_ip,
    }
    reg = None
    try:
        reg = run_registration(email=email, name=random.choice(
            ["Alex Turner", "Maria Lopez", "James Carter", "Emma Wilson", "David Chen",
             "Sophia Martin", "Daniel Scott", "Olivia Adams"]) if not args.name else args.name)
        row["register_result"] = {
            "success": bool(reg.get("success")),
            "error": reg.get("error"),
            "account_id": reg.get("account_id"),
        }
    except Exception as exc:
        row["register_result"] = {"success": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    row["checks"] = []

    # 4. 注册后对话预热(变量)
    if reg and reg.get("success") and args.warmup > 0:
        from core.conversation_warmup import warmup_account
        at = str(reg.get("access_token") or "")
        if at:
            warm = warmup_account(email, at, proxy="http://127.0.0.1:7897", messages=args.warmup)
            row["warmup_results"] = warm
        else:
            row["warmup_results"] = [{"ok": False, "status": "no_token"}]

    _save_row(row)
    ok = bool(row["register_result"].get("success"))
    logger.info("[Exp %s] 注册%s%s: %s", args.id, "成功" if ok else "失败",
                "(预热已发)" if row.get("warmup_results") else "", email)
    return 0 if ok else 1


def cheap_liveness(email: str, access_token: str, proxy: str) -> dict:
    """便宜存活探针:带 AT 调 /backend-api/me。200=活,401/403=疑似死亡。"""
    import requests
    try:
        r = requests.get(
            "https://chatgpt.com/backend-api/me",
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0",
                "Accept": "*/*",
            },
            proxies={"http": proxy, "https": proxy},
            timeout=20,
        )
        # 200=存活;401=死亡(token被拒,确证);403=探针出口被CF拦(结果不可判)
        return {"http": r.status_code, "alive": True if r.status_code == 200 else (False if r.status_code == 401 else None)}
    except Exception as exc:
        return {"http": 0, "alive": None, "error": str(exc)[:120]}


def cmd_check(args) -> int:
    rows = _load_all()
    from core import db
    proxy = "http://127.0.0.1:7897"
    now = time.time()
    print(f"{'实验ID':<10} {'邮箱':<32} {'注册后':<10} {'状态':<8} HTTP")
    for r in rows:
        if not r.get("register_result", {}).get("success"):
            print(f"{r['id']:<10} {r['email']:<32} {'-':<10} {'未成功':<8} -")
            continue
        account = db.get_account_by_email(r["email"])
        at = str((account or {}).get("access_token") or "")
        probe = cheap_liveness(r["email"], at, proxy) if at else {"http": -1, "alive": False}
        reg_ts = r.get("ts", "")
        age_h = ""
        try:
            t = datetime.strptime(reg_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            age_h = f"{(now - t) / 3600:.1f}h"
        except Exception:
            pass
        status = {True: "存活", False: "死亡", None: "探针被拦"}.get(probe.get("alive"), "未知")
        print(f"{r['id']:<10} {r['email']:<32} {age_h:<10} {status:<8} {probe.get('http')}")
        exp_id = r["id"]
        _update_row(exp_id, lambda row: row.setdefault("checks", []).append(
            {"t": _utcnow(), **probe}))
    return 0


def cmd_status(args) -> int:
    rows = _load_all()
    print(f"实验总数: {len(rows)}")
    for r in rows:
        last = (r.get("checks") or [{}])[-1]
        print(f"  {r['id']:<10} {r['email']:<32} 注册{'成功' if r.get('register_result',{}).get('success') else '失败'}"
              f" | 最后探针: {last.get('t','无')} http={last.get('http','-')} alive={last.get('alive')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("--id", required=True)
    run_p.add_argument("--node", required=True)
    run_p.add_argument("--no-password", action="store_true")
    run_p.add_argument("--warmup", type=int, default=1)
    run_p.add_argument("--dwell", default="")
    run_p.add_argument("--no-bootstrap", action="store_true")
    run_p.add_argument("--name", default="")
    run_p.set_defaults(func=cmd_run)
    chk = sub.add_parser("check")
    chk.set_defaults(func=cmd_check)
    st = sub.add_parser("status")
    st.set_defaults(func=cmd_status)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
