# -*- coding: utf-8 -*-
"""调试 export-selected 原始响应。"""
import logging
import json

logging.basicConfig(level=logging.CRITICAL)

import config

config.reload_all()
from core.email_manager_client import get_client

c = get_client()
c._ensure()

rv = c._post("/api/export/verify", {"password": c.password})
token = rv.get("verify_token") or (rv.get("data") or {}).get("verify_token")
print("token:", bool(token))

r2 = c._sess.post(
    c.base_url + "/api/accounts/export-selected",
    json={"account_ids": [652], "verify_token": token},
    headers={"X-CSRFToken": c._csrf} if c._csrf else {},
    timeout=60,
)
print("status:", r2.status_code, "| type:", r2.headers.get("Content-Type"))
print("body[:500]:", repr(r2.text[:500]))
