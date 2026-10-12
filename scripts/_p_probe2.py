# -*- coding: utf-8 -*-
"""移除探针 JS 里的 sess 读取(v2: 全部用单行 replace 避免引号地狱)。"""
import io

p = "core/cloakbrowser_registration.py"
s = io.open(p, encoding="utf-8").read()
lines = s.splitlines(keepends=True)

out = []
i = 0
removed = {"sess_js": 0, "sess_ret": 0, "py_ret": 0}
while i < len(lines):
    line = lines[i]
    stripped = line.strip()
    # JS: sess 计算段(4 行) —— 遇到起始行跳到 return 行
    if '"const sess=(location.hostname' in line:
        j = i
        while j < len(lines) and 'sess":null;"' not in lines[j]:
            j += 1
        removed["sess_js"] = 1
        i = j + 1 if j < len(lines) else j
        continue
    # JS return 里的 sess 字段
    if stripped == '" sess: sess,"':
        removed["sess_ret"] = 1
        i += 1
        continue
    # Python _probe 返回里的 session 字段
    if '"session": r.get("sess")' in line:
        removed["py_ret"] = 1
        i += 1
        continue
    out.append(line)
    i += 1

assert all(removed.values()), removed
io.open(p, "w", encoding="utf-8", newline="").write("".join(out))
print("移除:", removed)
