# -*- coding: utf-8 -*-
"""本地直连邮箱管理端: 扫描项目 toClaim 邮箱, 已用过的打标+移出。

不依赖 turb 代码, 独立运行: python scan_local.py [线程数]
- 判定: 收件箱(folder=all)有 OpenAI 邮件 = 用过 → 'gpt已用'标签 + 移出项目
- 超时自动退避, 管理端忙就让路
- 幂等: 已移出的自动跳过, 可随时中断重跑
"""
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = "http://10.10.10.3:9132"
PASSWORD = "Password4!"
KEY = "gpt"
WORKERS = int(sys.argv[1]) if len(sys.argv) > 1 else 2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger()

sess = requests.Session()
# 局域网直连, 不走代理
csrf = None
login_lock = threading.Lock()


def login():
    global csrf
    r = sess.post(f"{BASE}/login", json={"password": PASSWORD, "session_duration_days": "permanent"}, timeout=30)
    r.raise_for_status()
    r2 = sess.get(f"{BASE}/api/csrf-token", timeout=30)
    r2.raise_for_status()
    csrf = (r2.json() or {}).get("csrf_token")
    log.info("登录成功")


def api(method, path, payload=None, retry=True):
    global csrf
    headers = {"X-CSRFToken": csrf} if csrf else {}
    r = sess.request(method, f"{BASE}{path}", json=payload, headers=headers, timeout=60)
    if r.status_code in (401, 403) and retry:
        with login_lock:
            login()
        return api(method, path, payload, retry=False)
    r.raise_for_status()
    try:
        return r.json() or {}
    except Exception:
        return {}


login()

tags_raw = api("GET", "/api/tags")
tag_list = tags_raw.get("tags") or (tags_raw.get("data") or {}).get("tags") or []
TAG_ID = next((t.get("id") for t in tag_list if t.get("name") == "gpt已用"), None)
if not TAG_ID:
    r = api("POST", "/api/tags", {"name": "gpt已用", "color": "#8a8a8a"})
    d = r.get("data") or r.get("tag") or {}
    TAG_ID = d.get("id") or d.get("tag_id") or r.get("id")
log.info("TAG_ID=%s", TAG_ID)

accs_raw = api("GET", f"/api/projects/{KEY}/accounts")
data = accs_raw.get("data") or {}
accs = data.get("accounts") or data.get("items") or []
todo = [a for a in accs if (a.get("project_status") or a.get("status")) == "toClaim"]
log.info("待扫 %d / 项目共 %d", len(todo), len(accs))

cnt = {"used": 0, "clean": 0, "fail": 0, "done": 0}
lock = threading.Lock()


def fetch_emails(email, top=5):
    d = api("GET", f"/api/emails/{email}", None)
    return d.get("emails") or []


def work(a):
    email = str(a.get("email") or "")
    aid = a.get("account_id")
    try:
        mails = fetch_emails(email)
        used = any(
            ("openai" in str(m.get("from") or "").lower()
             or "openai" in str(m.get("subject") or "").lower()
             or "chatgpt" in str(m.get("subject") or "").lower())
            for m in mails
        )
    except Exception as exc:
        with lock:
            cnt["fail"] += 1
            cnt["done"] += 1
        log.info("FAIL %s %s", email, str(exc)[:60])
        time.sleep(20)
        return
    if used:
        try:
            api("POST", "/api/accounts/tags", {"account_ids": [aid], "tag_id": TAG_ID, "action": "add"})
            api("POST", f"/api/projects/{KEY}/remove-account",
                {"account_id": aid, "detail": "收件箱已有 OpenAI 邮件, 已用过"})
            with lock:
                cnt["used"] += 1
                cnt["done"] += 1
            log.info("USED %s (%d/%d)", email, cnt["done"], len(todo))
        except Exception as exc:
            log.info("MARK-FAIL %s %s", email, str(exc)[:60])
    else:
        with lock:
            cnt["clean"] += 1
            cnt["done"] += 1
            if cnt["done"] % 20 == 0:
                log.info("进度 %d/%d used=%d clean=%d fail=%d",
                         cnt["done"], len(todo), cnt["used"], cnt["clean"], cnt["fail"])
    time.sleep(1)


t0 = time.time()
with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    list(ex.map(work, todo))
dt = time.time() - t0
log.info("=== 完成: 用过 %d | 干净 %d | 失败 %d | 用时 %.0fmin ===",
         cnt["used"], cnt["clean"], cnt["fail"], dt / 60)
