# -*- coding: utf-8 -*-
"""第一步(虚空试放)冻结页验收:回读 freeze 标注,判两件事——
① 与 A4 纸面 [0,0,1170,825] 相交的模块框数(应为 0)
② 互相重叠的框对数(应为 0;≥1 mil² 面积才算,线接触放过)
用法: uv run python runs/step1-verify.py [P1]
"""
import json
import re
import subprocess
import sys

page = sys.argv[1] if len(sys.argv) > 1 else "P1"
PAPER = (0.0, 0.0, 1170.0, 825.0)

r = subprocess.run(["easyeda", "sch", "text-list", "--page", page],
                   capture_output=True, text=True, encoding="utf-8")
rep = json.loads(r.stdout or "{}")
texts = (rep.get("result") or {}).get("texts") or []

pat = re.compile(r"^(est )?(\S+) \((\d+),(\d+)\) (\d+)x(\d+)$")
frames = []  # (inst, x1, y1, x2, y2, is_est)
for t in texts:
    m = pat.match((t.get("content") or "").strip())
    if not m:
        continue
    x1, y2, w, h = (float(m.group(i)) for i in (3, 4, 5, 6))
    frames.append((m.group(2), x1, y2 - h, x1 + w, y2, m.group(1) is not None))

print(f"modules total={len(frames)} (est {sum(1 for f in frames if f[5])})")


def inter(a, b):  # a/b = (x1, y1, x2, y2)
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


inpaper = [f for f in frames if inter(f[1:5], PAPER)]
print(f"框与A4纸面相交: {len(inpaper)}")
for f in inpaper:
    print(f"  {f[0]} ({f[1]:.0f},{f[4]:.0f}) {f[3]-f[1]:.0f}x{f[4]-f[2]:.0f}")

ov = []
for i in range(len(frames)):
    for j in range(i + 1, len(frames)):
        a, b = frames[i], frames[j]
        ix = min(a[3], b[3]) - max(a[1], b[1])
        iy = min(a[4], b[4]) - max(a[2], b[2])
        if ix > 0 and iy > 0:
            ov.append((a[0], b[0], ix, iy))
print(f"重叠框对: {len(ov)}")
for a, b, ix, iy in ov:
    print(f"  {a} × {b}: {ix:.0f}x{iy:.0f}")

ok = not inpaper and not ov
print("VERDICT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
