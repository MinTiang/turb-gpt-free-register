# -*- coding: utf-8 -*-
"""连通性测试: 登录管理端, 列出项目与分组。"""
import logging

logging.basicConfig(level=logging.CRITICAL)

import config

config.reload_all()
from core.email_manager_client import get_client

c = get_client()
c._ensure()
print("登录 OK:", c.base_url)

projects = c._get("/api/projects")
d = projects.get("data") or {}
plist = d.get("projects") or []
if not plist:
    print("(尚无项目)")
for pr in plist:
    print(
        "项目 key=%s | 总%s 待领%s 领取中%s 成功%s 失败%s | 范围组=%s"
        % (pr.get("project_key"), pr.get("total_count"), pr.get("to_claim_count"),
           pr.get("claiming_count"), pr.get("done_count"), pr.get("failed_count"),
           pr.get("group_ids"))
    )

groups = c._get("/api/groups")
gd = groups.get("data") or {}
glist = gd.get("groups") or []
for g in glist:
    print("分组 id=%s name=%s" % (g.get("id"), g.get("name")))
