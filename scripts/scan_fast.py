# -*- coding: utf-8 -*-
"""分批扫描: 每批 N 个, 8 线程, 可断点续跑(已移出的自动跳过)。"""
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(level=logging.INFO, format="%(message)s")

import config

config.reload_all()
from core.email_manager_client import get_client

c = get_client()
c._ensure()
KEY = c.project_key

BATCH = int(sys.argv[1]) if len(sys.argv) > 1 else 30
WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else 3

_tags_raw = c._get("/api/tags")
_tag_list = _tags_raw.get("tags") or (_tags_raw.get("data") or {}).get("tags") or []
tag_id = None
for t in _tag_list:
    if str(t.get("name") or "") == "gpt已用":
        tag_id = t.get("id")
        break
if not tag_id:
    _r = c._post("/api/tags", {"name": "gpt已用", "color": "#8a8a8a"})
    _d = _r.get("data") or _r.get("tag") or {}
    tag_id = _d.get("id") or _d.get("tag_id") or _r.get("id")
print("tag_id=", tag_id, flush=True)

lock = threading.Lock()
cnt = {"used": 0, "clean": 0, "fail": 0}


def work(a):
    email = str(a.get("email") or "")
    aid = a.get("account_id")
    try:
        mails = c.fetch_emails(email, top=5)
        used = any(
            ("openai" in str(m.get("from") or "").lower()
             or "openai" in str(m.get("subject") or "").lower()
             or "chatgpt" in str(m.get("subject") or "").lower())
            for m in mails
        )
    except Exception:
        with lock:
            cnt["fail"] += 1
        time.sleep(30)   # 管理端过载/超时: 让路半分钟再继续(单worker部署, 扛不住并发)
        return
    time.sleep(1)        # 串行节流: 每号 1s 间隔, 与注册任务和平共处
    if used:
        try:
            if tag_id:
                c._post("/api/accounts/tags", {"account_ids": [aid], "tag_id": tag_id, "action": "add"})
            c._post(f"/api/projects/{KEY}/remove-account",
                    {"account_id": aid, "detail": "收件箱已有 OpenAI 邮件, 已用过"})
            with lock:
                cnt["used"] += 1
        except Exception:
            pass
    else:
        with lock:
            cnt["clean"] += 1


accs = (c._get(f"/api/projects/{KEY}/accounts").get("data") or {})
accs = accs.get("accounts") or accs.get("items") or []
todo = [a for a in accs if (a.get("project_status") or a.get("status")) == "toClaim"]
batch = todo[:BATCH]
t0 = time.time()
with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    list(ex.map(work, batch))
dt = time.time() - t0
print(f"本批 {len(batch)} 用时 {dt:.0f}s ({len(batch) / max(dt, 1) * 60:.0f}/min) | "
      f"used={cnt['used']} clean={cnt['clean']} fail={cnt['fail']} | 剩余 {len(todo) - len(batch)}", flush=True)
