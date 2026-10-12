import glob
import os

target = None
for f in sorted(glob.glob("/app/注册日志/*.log"), key=os.path.getmtime, reverse=True):
    content = open(f, encoding="utf-8", errors="replace").read()
    if "pw_new" in content and "2 次后" in content:
        target = f
        break

if not target:
    print("没找到 pw_new 失败日志")
else:
    print("==", os.path.basename(target))
    lines = open(target, encoding="utf-8", errors="replace").readlines()
    keys = ("pw_new", "密码页", "领取", "生成并填入", "状态=", "换粘性出口", "出口IP")
    hits = [l.rstrip() for l in lines if any(k in l for k in keys)]
    for l in hits[:16]:
        print(l[:145])
