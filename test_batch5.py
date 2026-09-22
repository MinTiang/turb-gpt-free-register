"""5 个号的批量测试: 统计 clearance 开启后的成功率。"""
import logging, sys, json, time, io
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S',
                    handlers=[logging.StreamHandler(sys.stdout)])
import main

results = []
t_start = time.time()

for i in range(1, 6):
    print(f"\n{'='*70}\n### 第 {i}/5 个号 ###\n{'='*70}", flush=True)
    t0 = time.time()
    try:
        email, name, birthday = main.prepare_registration_inputs()
    except Exception as e:
        print(f"  参数准备失败: {e}", flush=True)
        results.append({"idx": i, "email": "?", "ok": False, "err": f"prepare:{type(e).__name__}", "sec": 0})
        continue
    try:
        r = main.run_registration(email=email, name=name, birthday=birthday)
    except Exception as e:
        r = {"success": False, "error": f"{type(e).__name__}: {str(e)[:150]}"}
    dt = time.time() - t0
    ok = bool(isinstance(r, dict) and r.get("success"))
    codex = (r or {}).get("codex") or {}
    codex_ok = bool(codex.get("ok"))
    err = str((r or {}).get("error") or "")[:110]
    results.append({"idx": i, "email": email, "ok": ok, "codex_ok": codex_ok, "err": err, "sec": dt})
    print(f"\n>>> [{i}] {'✅成功' if ok else '❌失败'} ({dt:.0f}s) codex={'✓' if codex_ok else '✗'} {err if not ok else ''}", flush=True)
    if i < 5:
        print("  间隔 20s 后继续...", flush=True)
        time.sleep(20)

print(f"\n{'='*70}\n### 汇总 (总耗时 {time.time()-t_start:.0f}s) ###\n{'='*70}")
ok_n = sum(1 for r in results if r["ok"])
codex_n = sum(1 for r in results if r.get("codex_ok"))
print(f"注册成功: {ok_n}/5    Codex授权成功: {codex_n}/5\n")
for r in results:
    print(f"  [{r['idx']}] {'✅' if r['ok'] else '❌'} {r['email'][:34]:34s} {r['sec']:5.0f}s codex={'✓' if r.get('codex_ok') else '✗'} {r['err']}")
io.open("docs/experiments/batch5-result.json", "w", encoding="utf-8").write(
    json.dumps({"when": time.strftime("%Y-%m-%d %H:%M:%S"), "results": results}, ensure_ascii=False, indent=1))
