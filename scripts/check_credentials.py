# -*- coding: utf-8 -*-
"""逐步验证管理端四件套获取。"""
import logging
import json

logging.basicConfig(level=logging.CRITICAL)

import config

config.reload_all()
from core.email_manager_client import get_client

c = get_client()
c._ensure()

r = c._get("/api/accounts/search", {"q": "audtbmcx329257@outlook.com", "limit": 5})
accs = r.get("accounts") or []
acc = accs[0] if accs else None
print("search hit id:", acc and acc.get("id"))

detail = c._get(f"/api/accounts/{acc['id']}")
a = (detail.get("data") or {}).get("account") or {}
print("detail:", bool(a),
      "| refresh_token:", bool(a.get("refresh_token")),
      "| client_id:", bool(a.get("client_id")))

sec = c._post(f"/api/accounts/{acc['id']}/secrets",
              {"password": c.password, "field": "password"})
print("secrets:", json.dumps(sec, ensure_ascii=False)[:200])

cred = c.get_account_credentials("audtbmcx329257@outlook.com")
print("get_account_credentials:", {k: bool(v) for k, v in (cred or {}).items()})
