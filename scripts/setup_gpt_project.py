# -*- coding: utf-8 -*-
"""创建 GPT 注册项目(scope=gpt free号池 分组) 并列出结果。"""
import logging

logging.basicConfig(level=logging.CRITICAL)

import config

config.reload_all()
from core.email_manager_client import get_client

c = get_client()
c._ensure()

groups = c._get("/api/groups")
gd = groups.get("data") or {}
target = None
for g in gd.get("groups") or []:
    if True:
        print("候选分组:", g.get("id"), g.get("name"), "含子", g.get("descendant_account_count"))
        if target is None or (g.get("descendant_account_count") or 0) > (target.get("descendant_account_count") or 0):
            target = g

if target is None:
    print("没找到含 gpt 的分组, 请在管理端建分组后再跑")
    raise SystemExit(1)

gid = target["id"]
print("用分组:", target["name"], "id=", gid)

r = c._post("/api/projects/start", {
    "project_key": "gpt",
    "name": "GPT 注册",
    "description": "turb 注册项目: 领取→注册→成功/失败回写",
    "group_ids": [gid],
})
d = r.get("data") or {}
print("项目启动:", r.get("success"), "| created:", d.get("created"),
      "| 本次补入:", d.get("added_count"),
      "| 待领:", d.get("to_claim_count"),
      "| 总:", d.get("total_count"))
