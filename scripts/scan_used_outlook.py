# -*- coding: utf-8 -*-
"""检查邮箱池: 收件箱里已有 OpenAI 邮件的标停用。

用法: python scripts/scan_used_outlook.py [数量上限]
只读 Microsoft Graph, 只写本地 email_pool 的 payload.status。
"""
import json
import sqlite3
import sys

from core.outlook_client import (
    OutlookAccount,
    _ms_http,
    _ms_access_token,
    _fetch_graph_messages,
)

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 10
DB = "/app/state/turb.sqlite3"


def has_openai(messages) -> bool:
    for m in messages:
        frm = str(m.get("from") or "").lower()
        sub = str(m.get("subject") or "").lower()
        if "openai" in frm or "chatgpt" in sub or "openai" in sub:
            return True
    return False


def main() -> None:
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT id, payload FROM email_pool WHERE source='outlook' AND status='available' ORDER BY id"
    ).fetchall()
    used = clean = dead = 0
    for pid, payload in rows[:LIMIT]:
        d = json.loads(payload)
        email = d.get("email")
        acc = OutlookAccount(
            email=email,
            password=str(d.get("password") or ""),
            client_id=str(d.get("client_id") or ""),
            refresh_token=str(d.get("refresh_token") or ""),
        )
        try:
            http = _ms_http()
            token, _kind = _ms_access_token(acc, http)
            msgs = _fetch_graph_messages(http, token)
        except Exception as exc:
            dead += 1
            d["status"] = "disabled"
            d["note"] = f"扫描: 取信失败 {type(exc).__name__}"
            conn.execute(
                "UPDATE email_pool SET payload=? WHERE id=?",
                (json.dumps(d, ensure_ascii=False), pid),
            )
            conn.commit()
            print(f"DEAD   {email}  {type(exc).__name__}: {str(exc)[:60]}", flush=True)
            continue
        if has_openai(msgs):
            d["status"] = "disabled"
            conn.execute(
                "UPDATE email_pool SET payload=? WHERE id=?",
                (json.dumps(d, ensure_ascii=False), pid),
            )
            conn.commit()
            used += 1
            print(f"USED   {email}", flush=True)
        else:
            clean += 1
            print(f"CLEAN  {email}", flush=True)
    print(f"--- 扫描 {min(LIMIT, len(rows))}/{len(rows)} | 已用(停用) {used} | 干净 {clean} | 取信失败 {dead}")
    conn.close()


if __name__ == "__main__":
    main()
