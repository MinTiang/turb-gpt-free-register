import glob
import os

# eead5ed3: pw_new 卡死日志。看密码填入那两轮里页面 snapshot / 提交按钮是否点到了
target = "/app/注册日志/eead5ed3-c621-4b07-b689-2ec227162778.log"
lines = open(target, encoding="utf-8", errors="replace").readlines()
# 打印 16:38:23 到 16:39:24 之间全部行(密码页第一次动作到失败)
inside = False
for l in lines:
    if "16:38:23" in l:
        inside = True
    if inside:
        print(l.rstrip()[:150])
    if "16:39:24" in l and "失败" in l:
        break
