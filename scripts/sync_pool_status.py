# -*- coding: utf-8 -*-
"""把 payload 里的 status 同步到 email_pool.status 列(取号读的是列)。"""
import json
import sqlite3

c = sqlite3.connect("/app/state/turb.sqlite3")
rows = c.execute("SELECT id, status, payload FROM email_pool WHERE source='outlook'").fetchall()
fixed = 0
for pid, col_status, payload in rows:
    d = json.loads(payload)
    want = str(d.get("status") or "")
    if want and want != col_status:
        c.execute("UPDATE email_pool SET status=? WHERE id=?", (want, pid))
        fixed += 1
c.commit()
left = c.execute(
    "SELECT COUNT(*) FROM email_pool WHERE source='outlook' AND status='available'"
).fetchone()[0]
disabled = c.execute(
    "SELECT COUNT(*) FROM email_pool WHERE source='outlook' AND status='disabled'"
).fetchone()[0]
print(f"列已同步: {fixed}")
print(f"available: {left} | disabled: {disabled}")
c.close()
