# -*- coding: utf-8 -*-
"""给项目 gpt 里所有 toClaim(干净待领)账号打 gpt可用 标签。"""
import logging

logging.basicConfig(level=logging.CRITICAL)

import config

config.reload_all()
from core.email_manager_client import get_client

c = get_client()
c._ensure()
KEY = c.project_key
ok_tag = c.ensure_tag("gpt可用", "#00bcf2")

accs = (c._get(f"/api/projects/{KEY}/accounts").get("data") or {})
accs = accs.get("accounts") or accs.get("items") or []
n = 0
for a in accs:
    if (a.get("project_status") or a.get("status")) == "toClaim":
        try:
            c.account_tag(a.get("account_id"), ok_tag, "add")
            n += 1
        except Exception as exc:
            print("失败:", a.get("email"), str(exc)[:60])
print(f"已给 {n} 个干净账号打 gpt可用 标签")
