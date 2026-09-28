# -*- coding: utf-8 -*-
"""把 turb 本地池里已消费(used/failed/disabled)的邮箱, 在管理端项目里
打 'gpt已用' 标签并移出领取范围, 只留没用过的新号可被领取。"""
import logging

logging.basicConfig(level=logging.CRITICAL)

import sqlite3

import config

config.reload_all()
from core.email_manager_client import get_client

c = get_client()
c._ensure()
KEY = c.project_key

# 1) 本地池历史状态
conn = sqlite3.connect("/app/state/turb.sqlite3")
local = {}
for email, status in conn.execute(
    "SELECT email, status FROM email_pool WHERE source='outlook'"
):
    local[str(email).lower()] = status
conn.close()

# 2) 确保标签存在
tags = (c._get("/api/tags").get("data") or {})
tag_list = tags.get("tags") or tags.get("data") or []
tag_id = None
for t in tag_list:
    if str(t.get("name") or "") == "gpt已用":
        tag_id = t.get("id")
        break
if not tag_id:
    r = c._post("/api/tags", {"name": "gpt已用", "color": "#8a8a8a"})
    d = r.get("data") or {}
    tag_id = d.get("id") or d.get("tag_id")
print("标签 gpt已用 tag_id =", tag_id)

# 3) 遍历项目账号: toClaim 且本地已消费 → 打标 + 移出项目
accounts = c._get(f"/api/projects/{KEY}/accounts")
data = accounts.get("data") or {}
accs = data.get("accounts") or data.get("items") or []
print("项目账号:", len(accs))

marked = removed = skipped = clean = 0
for a in accs:
    email = str(a.get("email") or "").lower()
    st = a.get("project_status") or a.get("status")
    aid = a.get("account_id")
    lstatus = local.get(email)
    if lstatus in ("used", "failed", "disabled"):
        if tag_id:
            c._post("/api/accounts/tags",
                    {"account_ids": [aid], "tag_id": tag_id, "action": "add"})
        if st == "toClaim":
            c._post(f"/api/projects/{KEY}/remove-account",
                    {"account_id": aid, "detail": "本地池已消费, 移出领取范围"})
            removed += 1
        marked += 1
    elif st == "toClaim":
        clean += 1
    else:
        skipped += 1

print(f"已消费标记 {marked} | 其中移出领取范围 {removed} | 干净待领 {clean} | 其它 {skipped}")
