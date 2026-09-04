"""run6 拒排物理分析:逐页组包络(高/宽) vs 615 可用带 + 行 packing 模拟。

用法: uv run python run/analyze-groups.py <page> [...]   # 如 P1 P2 P6
"""
from __future__ import annotations

import json
import subprocess
import sys

BAND = (12, 198, 1158, 813)  # run5/6 实测 arrange 可用区 x1,y1,x2,y2
GAPS = (80, 60)


def run_cli(args: list[str]) -> str:
    r = subprocess.run(
        ["uv", "run", "easyeda", *args], capture_output=True, text=True, encoding="utf-8"
    )
    return r.stdout or ""


def page_groups(page: str):
    cl = json.loads(run_cli(["sch", "clusters", "--json", "--doc", page]) or "{}")
    gl = json.loads(run_cli(["sch", "group", "list", "--json", "--doc", page]) or "{}")
    boxes = {c["designator"]: c["box"] for c in cl.get("clusters", []) if c.get("designator")}
    groups = [g for gs in (gl.get("groupsByPage") or {}).values() for g in gs]
    out = []
    for g in groups:
        members = [m.get("designator") for m in (g.get("members") or []) if m.get("designator")]
        mb = [boxes[d] for d in members if d in boxes]
        if not mb:
            out.append({"id": g.get("id") or g.get("name"), "name": g.get("name"),
                        "n": len(members), "missing": [d for d in members if d not in boxes]})
            continue
        x1, y1 = min(b["minX"] for b in mb), min(b["minY"] for b in mb)
        x2, y2 = max(b["maxX"] for b in mb), max(b["maxY"] for b in mb)
        out.append({"id": g.get("id") or g.get("name"), "name": g.get("name"), "n": len(members),
                    "w": round(x2 - x1, 1), "h": round(y2 - y1, 1),
                    "x": round(x1, 1), "y": round(y1, 1)})
    return out, cl.get("findings", [])


def pack_rows(groups: list[dict], gap: int, band_w: float, band_h: float):
    """模拟 arrange 行 packing:按当前顺序贪心装行(宽进位),返回所需总高。"""
    rows, cur_w, cur_h = [], 0.0, 0.0
    for g in groups:
        w, h = g["w"], g["h"]
        if cur_w > 0 and cur_w + gap + w > band_w:
            rows.append(cur_h)
            cur_w, cur_h = w, h
        else:
            cur_w = (cur_w + gap + w) if cur_w > 0 else w
            cur_h = max(cur_h, h)
    rows.append(cur_h)
    return sum(rows) + gap * (len(rows) - 1), rows


def main(pages: list[str]) -> int:
    for p in pages:
        gs, findings = page_groups(p)
        print(f"== {p}: {len(gs)} 组, findings={[(f.get('type'), f.get('a'), f.get('b')) for f in findings]}")
        for g in sorted(gs, key=lambda g: -(g.get("h", 0))):
            print(f"   {g['id']:>4} {str(g.get('name'))[:28]:<28} n={g['n']} "
                  f"w={g.get('w')} h={g.get('h')}" + (f" [无box:{g['missing']}]" if "missing" in g else ""))
        sized = [g for g in gs if "h" in g]
        bw, bh = BAND[2] - BAND[0], BAND[3] - BAND[1]
        for gap in GAPS:
            need, rows = pack_rows(sized, gap, bw, bh)
            fit = "FIT" if need <= bh else f"超 {round(need - bh)}"
            print(f"   gap={gap}: 行高={rows} 总需={round(need)} / 带 {round(bh)} → {fit}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["P1", "P2", "P6"]))
