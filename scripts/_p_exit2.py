# -*- coding: utf-8 -*-
"""pw_new/email 卡死 → 出口类失败(换出口重试)。行级替换, 缩进对齐现场。"""
import io

p = "core/cloakbrowser_registration.py"
raw = io.open(p, encoding="utf-8", newline="").read()
NL = "\r\n" if "\r\n" in raw else "\n"
lines = raw.splitlines()

def find(sub, start=0):
    for n in range(start, len(lines)):
        if sub in lines[n]:
            return n
    raise SystemExit(f"not found: {sub[:50]}")

# 定位 raise RuntimeError( 两行(330-331)
i_raise = find("raise RuntimeError(")
assert "次后仍未前进" in lines[i_raise + 1], lines[i_raise + 1]
# 缩进取自现场 raise 行
ind = lines[i_raise][: len(lines[i_raise]) - len(lines[i_raise].lstrip())]
new = [
    f'{ind}if st in ("pw_new", "email"):',
    f'{ind}    # 密码页/邮箱页填了不动 = 页面内隐形 CF 质询阻塞表单',
    f'{ind}    # (实测烂出口单任务 13 次质询), 判出口类让上层立即换粘性出口,',
    f'{ind}    # 不烧完整 5 分钟。',
    f'{ind}    raise RuntimeError(',
    f'{ind}        f"页面[{{st}}]处理 {{state_tries.get(st, 0)}} 次后仍未前进,"',
    f'{ind}        "未渲染出可用元素,登录页超过阻塞: 换出口重试")',
    f'{ind}raise RuntimeError(',
    f'{ind}    f"页面[{{st}}]处理 {{state_tries.get(st, 0)}} 次后仍未前进,任务失败(邮箱回池,由连续失败机制退役)")',
]
lines[i_raise:i_raise + 2] = new

io.open(p, "w", encoding="utf-8", newline="").write(NL.join(lines) + NL)
print("分流完成")
