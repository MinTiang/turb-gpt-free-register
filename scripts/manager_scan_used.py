# -*- coding: utf-8 -*-
"""并行扫描版: 8 线程收信判定, 幂等可重跑(已移出的自动跳过)。"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger()

import config

config.reload_all()
from core.email_manager_client import get_client

c = get_client()
c._ensure()
KEY = c.project_key

tag_id = None
for t in ((c._get("/api/tags").get("data") or {}).get("tags") or []):
    if str(t.get("name") or "") == "gpt已用":
        tag_id = t.get("id")
        break
if not tag_id:
    r = c._post("/api/tags", {"name": "gpt已用", "color": "#8a8a8a"})
    d = r.get("data") or {}
    tag_id = d.get("id") or d.get("tag_id")
print("tag_id =", tag_id, flush=True)

accs = (c._get(f"/api/projects/{KEY}/accounts").get("data") or {})
accs = accs.get("accounts") or accs.get("items") or []
todo = [a for a in accs if (a.get("project_status") or a.get("status")) == "toClaim"]
print(f"待扫 {len(todo)}/{len(accs)}", flush=True)

lock = threading.Lock()
counters = {"used": 0, "clean": 0, "fail": 0, "done": 0}


def work(a):
    email = str(a.get("email") or "")
    aid = a.get("account_id")
    try:
        mails = c.fetch_emails(email, top=5)
        is_used = any(
            ("openai" in str(m.get("from") or "").lower()
             or "openai" in str(m.get("subject") or "").lower()
             or "chatgpt" in str(m.get("subject") or "").lower())
            for m in mails
        )
    except Exception as exc:
        with lock:
            counters["fail"] += 1
        log.info("FAIL %s %s", email, str(exc)[:60])
        return
    if is_used:
        if tag_id:
            try:
                c._post("/api/accounts/tags", {"account_ids": [aid], "tag_id": tag_id, "action": "add"})
            except Exception:
                pass
        try:
            c._post(f"/api/projects/{KEY}/remove-account",
                    {"account_id": aid, "detail": "收件箱已有 OpenAI 邮件, 判定已用过"})
        except Exception:
            pass
        with lock:
            counters["used"] += 1
            counters["done"] += 1
            log.info("USED %s (%d)", email, counters["done"])
    else:
        with lock:
            counters["clean"] += 1
            counters["done"] += 1
            if counters["done"] % 25 == 0:
                log.info("进度 %d/%d | used=%d clean=%d fail=%d", counters["done"], len(todo), counters["used"], counters["clean"], counters["fail"])


with ThreadPoolExecutor(max_workers=8) as ex:
    list(ex.map(work, todo))

print(f"=== 完成: 用过 {counters['used']} | 干净 {counters['clean']} | 失败 {counters['fail']} ===", flush=True)
