# -*- coding: utf-8 -*-
"""块框"虚大"量化:clusters 的 body(本体) vs box(含 netport 文字/桩线)。

在试放页 P1 上,按持久组重建块→器件映射,算每块:
  ink_box  = ∪成员 box   (装箱用的框,蓝色虚线框同口径)
  body_box = ∪成员 body  (器件本体紧凑框)
  void%    = 1 - body面积/ink面积
并把 body_box 画上 P1(红色虚线),与已有蓝色框/标注并排目检。

用法: ./.venv/Scripts/python.exe runs/block-void.py [P1]
"""
import json
import os
import subprocess
import sys

PAGE = sys.argv[1] if len(sys.argv) > 1 else "P1"


def eda(*args: str, ok_rc1: bool = False) -> str:
    r = subprocess.run(["easyeda", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    if r.returncode != 0 and not (ok_rc1 and r.returncode == 1):
        raise SystemExit(f"[block-void] easyeda {' '.join(args[:3])} rc={r.returncode}\n{r.stderr[:500]}")
    return r.stdout


def main() -> int:
    # 1) 块→器件:持久组名 "block(designator)/子组" → 前缀即块实例
    gj = json.loads(eda("sch", "group", "list", "--json", "--doc", PAGE))
    blocks: dict[str, list[str]] = {}
    for g in [g for pg in gj["groupsByPage"].values() for g in pg]:
        key = g["name"].split("/")[0]
        blocks.setdefault(key, [])
        for m in g.get("members") or []:
            d = m.get("designator")
            if d and d not in blocks[key]:
                blocks[key].append(d)

    # 2) 每器件 body/box
    cj = json.loads(eda("sch", "clusters", "--json", "--doc", PAGE, ok_rc1=True))
    comp = {c["designator"]: c for c in cj["clusters"] if c.get("designator")}

    rows = []
    for blk, members in sorted(blocks.items()):
        mb = [comp[d] for d in members if d in comp]
        if not mb:
            continue
        ix1 = min(c["box"]["minX"] for c in mb); iy1 = min(c["box"]["minY"] for c in mb)
        ix2 = max(c["box"]["maxX"] for c in mb); iy2 = max(c["box"]["maxY"] for c in mb)
        bx1 = min(c["body"]["minX"] for c in mb); by1 = min(c["body"]["minY"] for c in mb)
        bx2 = max(c["body"]["maxX"] for c in mb); by2 = max(c["body"]["maxY"] for c in mb)
        iw, ih = ix2 - ix1, iy2 - iy1
        bw, bh = bx2 - bx1, by2 - by1
        void = 1.0 - (bw * bh) / (iw * ih) if iw * ih else 0.0
        worst = max(mb, key=lambda c: (c["box"]["maxX"] - c["box"]["minX"]) * (c["box"]["maxY"] - c["box"]["minY"]))
        ww = worst["box"]["maxX"] - worst["box"]["minX"]
        wh = worst["box"]["maxY"] - worst["box"]["minY"]
        rows.append((blk, len(members), iw, ih, bw, bh, void, worst["designator"], ww, wh,
                     bx1, by1, bx2, by2))
    rows.sort(key=lambda r: -r[6])
    print(f"{'块实例':38s} {'件':>3s} {'ink框':>11s} {'body框':>11s} {'虚大':>6s} {'最胖件':>16s}")
    for blk, n, iw, ih, bw, bh, void, wd, ww, wh, *_ in rows:
        print(f"{blk[:38]:38s} {n:3d} {iw:5.0f}x{ih:<5.0f} {bw:5.0f}x{bh:<5.0f} {void*100:5.1f}% {wd} {ww:.0f}x{wh:.0f}")
    tot_ink = sum(r[2] * r[3] for r in rows)
    tot_body = sum(r[4] * r[5] for r in rows)
    print(f"\n合计: ink {tot_ink/1e3:.0f}k vs body {tot_body/1e3:.0f}k (画布单位^2) -> 总虚大 {1 - tot_body/tot_ink:.1%}")

    # 3) 红色紧凑框画上试放页(与蓝色 ink 框对照)
    pages = json.loads(eda("sch", "pages"))
    uuid = next((p.get("uuid") for p in pages.get("result", {}).get("pages", [])
                 if str(p.get("name")) == PAGE), "")
    if not uuid:
        print("[block-void] 找不到页 uuid,跳过画框(数据表已打印)")
        return 0
    eda("sch", "open", "--page", uuid)
    rects = []
    for blk, n, iw, ih, bw, bh, void, wd, ww, wh, bx1, by1, bx2, by2 in rows:
        top = by2
        label = f"tight {blk} ({bx1:.0f},{top:.0f}) {bw:.0f}x{bh:.0f}"
        rects.append(
            f"try{{var r=eda.sch_PrimitiveRectangle.create({bx1:.1f},{top:.1f},{bw:.1f},{bh:.1f},0,0,'#D62728',null,1,1);"
            f"var t=eda.sch_PrimitiveText.create({bx1 + 4:.1f},{top - 14:.1f},{json.dumps(label)},0,'#D62728',null,14);"
            f"ids.push(r.id,t.id)}}catch(e){{for(var i of ids)eda.sch_PrimitiveObject.delete([i]);throw e}}")
    code = "var ids=[];" + "".join(rects) + "ids.join(',')"
    out = eda("debug", "exec", "--code", code)
    try:
        ok = json.loads(out).get("result", {}).get("value")
        print(f"[block-void] 红色紧凑框已画 {len(rows)} 组: {str(ok)[:80]}")
    except Exception:
        print(f"[block-void] 画框返回解析失败(原始): {out[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
