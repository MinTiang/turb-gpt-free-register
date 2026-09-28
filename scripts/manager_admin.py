# -*- coding: utf-8 -*-
"""outlookEmail 项目管理 CLI(该项目前端暂无项目入口, 用此脚本管理)。

用法(在 turb 容器内):
  python /tmp/manager_admin.py stats                 # 项目计数看板
  python /tmp/manager_admin.py list-failed           # 列出失败邮箱
  python /tmp/manager_admin.py reset-failed          # 失败全部重置回可领取
  python /tmp/manager_admin.py reset-one <email>     # 重置单个失败邮箱
  python /tmp/manager_admin.py list-claiming         # 列出领取中(含租期过期未回写的)
  python /tmp/manager_admin.py release <email>       # 释放单个领取中的邮箱
  python /tmp/manager_admin.py set-scope <group_id>  # 调整项目范围为指定分组
"""
import sys

import logging

logging.basicConfig(level=logging.CRITICAL)

import config

config.reload_all()
from core.email_manager_client import get_client

c = get_client()
c._ensure()
KEY = c.project_key


def _accounts() -> list:
    r = c._get(f"/api/projects/{KEY}/accounts")
    d = r.get("data") or {}
    return d.get("accounts") or d.get("items") or []


def stats() -> None:
    d = ((c._get(f"/api/projects/{KEY}").get("data") or {}).get("project")) or {}
    print(
        f"项目 {d.get('name')} (key={d.get('project_key')})\n"
        f"  总量 {d.get('total_count')} | 待领 {d.get('to_claim_count')} | "
        f"领取中 {d.get('claiming_count')} | 成功 {d.get('done_count')} | "
        f"失败 {d.get('failed_count')} | 移出 {d.get('removed_count')}"
    )


def _find(email: str) -> dict:
    for a in _accounts():
        if str(a.get("email") or "").lower() == email.lower():
            return a
    raise SystemExit(f"项目里找不到 {email}")


def list_failed() -> None:
    for a in _accounts():
        if a.get("project_status") == "failed":
            print(a.get("email"), "|", str(a.get("project_note") or a.get("remark") or "")[:80])


def reset_failed() -> None:
    n = 0
    for a in _accounts():
        if a.get("project_status") == "failed":
            c._post(f"/api/projects/{KEY}/reset-failed",
                    {"account_id": a.get("account_id"), "detail": "批量重置"})
            n += 1
    print(f"已重置 {n} 个失败邮箱回可领取")


def reset_one(email: str) -> None:
    a = _find(email)
    c._post(f"/api/projects/{KEY}/reset-failed",
            {"account_id": a.get("account_id"), "detail": "手动重置"})
    print("已重置", email)


def list_claiming() -> None:
    for a in _accounts():
        if a.get("project_status") == "claiming":
            print(a.get("email"), "| 领取时间:", a.get("claimed_at"))


def release(email: str) -> None:
    a = _find(email)
    c._post(f"/api/projects/{KEY}/release",
            {"account_id": a.get("account_id"), "detail": "手动释放"})
    print("已释放", email)


def set_scope(group_id: int) -> None:
    r = c._post("/api/projects/start", {"project_key": KEY, "group_ids": [group_id]})
    d = ((r.get("data") or {}).get("project")) or {}
    print(f"范围已更新: 总 {d.get('total_count')} | 待领 {d.get('to_claim_count')}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "stats":
        stats()
    elif cmd == "list-failed":
        list_failed()
    elif cmd == "reset-failed":
        reset_failed()
    elif cmd == "reset-one":
        reset_one(sys.argv[2])
    elif cmd == "list-claiming":
        list_claiming()
    elif cmd == "release":
        release(sys.argv[2])
    elif cmd == "set-scope":
        set_scope(int(sys.argv[2]))
    else:
        print(__doc__)
