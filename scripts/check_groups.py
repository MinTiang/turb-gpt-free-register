# -*- coding: utf-8 -*-
"""看 groups 原始响应结构。"""
import logging, json

logging.basicConfig(level=logging.CRITICAL)

import config

config.reload_all()
from core.email_manager_client import get_client

c = get_client()
c._ensure()
raw = c._get("/api/groups")
print(json.dumps(raw, ensure_ascii=False, indent=1)[:1500])
