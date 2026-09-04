# -*- coding: utf-8 -*-
"""路由器离线诊断:对冻结 P1 的每个内部网,重放 _route_pin_pair 的候选裁定,
打印每条候选被哪个障碍挡下(本体/他脚/他标记/共线桩)。用于收紧容差前的归因。

用法: ./.venv/Scripts/python.exe runs/route-diag.py
"""
import json
import re
import subprocess
import sys

sys.path.insert(0, "src")
from edaloop.loop.controller import (  # noqa: E402
    _INTERNAL_NET_RE, _leg_hits_rect, _leg_near_point, _legs_collinear_overlap,
)


def eda(*args: str) -> str:
    r = subprocess.run(["easyeda", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    if r.returncode != 0:
        raise SystemExit(f"[route-diag] rc={r.returncode}: {r.stderr[:300]}")
    return r.stdout


def snap(v: float) -> float:
    return round(v / 10.0) * 10.0


def diag(p1, p2, ax1, ax2, fb, fp, fm, wl) -> list[str]:
    x1, y1 = p1
    x2, y2 = p2
    cands = []
    if (x1 == x2 or y1 == y2) and (x1 != x2 or y1 != y2):
        ax = "v" if x1 == x2 else "h"
        if ax != ax1 and ax != ax2:
            cands.append(("straight", [[x1, y1], [x2, y2]]))
    xm, ym = snap((x1 + x2) / 2.0), snap((y1 + y2) / 2.0)
    cands += [
        ("L1-HV", [[x1, y1], [x2, y1], [x2, y2]]),
        ("L2-VH", [[x1, y1], [x1, y2], [x2, y2]]),
        ("Z1-HVH", [[x1, y1], [xm, y1], [xm, y2], [x2, y2]]),
        ("Z2-VHV", [[x1, y1], [x1, ym], [x2, ym], [x2, y2]]),
    ]
    verdicts = []
    for name, rt in cands:
        pts = [pt for i, pt in enumerate(rt) if i == 0 or pt != rt[i - 1]]
        if len(pts) < 2:
            verdicts.append(f"{name}: 退化")
            continue
        legs = list(zip(pts, pts[1:]))
        fax = "h" if legs[0][0][1] == legs[0][1][1] else "v"
        lax = "h" if legs[-1][0][1] == legs[-1][1][1] else "v"
        if fax == ax1 or lax == ax2:
            verdicts.append(f"{name}: 共线约束(fax={fax}/ax1={ax1}, lax={lax}/ax2={ax2})")
            continue
        reason = ""
        for a, b in legs:
            for d, r in fb:
                if _leg_hits_rect(a[0], a[1], b[0], b[1], r):
                    reason = f"本体{d}"
                    break
            if reason:
                break
            for px, py, pd in fp:
                if _leg_near_point(a[0], a[1], b[0], b[1], px, py):
                    reason = f"他脚{pd}({px:.0f},{py:.0f})"
                    break
            if reason:
                break
            for mx, my, mn in fm:
                if _leg_near_point(a[0], a[1], b[0], b[1], mx, my, 5.0):
                    reason = f"标记{mn}({mx:.0f},{my:.0f})"
                    break
            if reason:
                break
            for w2 in wl:
                if _legs_collinear_overlap(a, b, w2[:2], w2[2:]):
                    reason = "共线线段"
                    break
            if reason:
                break
        verdicts.append(f"{name}: " + (reason or "OK"))
    return verdicts


def main() -> int:
    rep = json.loads(eda("sch", "list", "--page", "P1", "--include-pins", "--include-bbox"))
    comps = rep["result"]["components"]
    parts = {c["designator"]: c for c in comps
             if c.get("componentType") == "part" and c.get("designator")}
    marks = [c for c in comps if c.get("componentType") in ("netport", "netflag", "netlabel")]
    nets: dict[str, list] = {}
    for d, c in parts.items():
        for p in c.get("pins") or []:
            n = p.get("net")
            if n and _INTERNAL_NET_RE.match(str(n)):
                nets.setdefault(str(n), []).append((d, str(p.get("pinNumber")), float(p["x"]), float(p["y"])))
    mby: dict[str, list] = {}
    for m in marks:
        mby.setdefault(str(m.get("net") or m.get("name")), []).append(
            (float(m["x"]), float(m["y"]), m["componentType"]))
    bodies = {d: (float(c["bbox"]["minX"]), float(c["bbox"]["minY"]),
                  float(c["bbox"]["maxX"]), float(c["bbox"]["maxY"]))
              for d, c in parts.items() if isinstance(c.get("bbox"), dict)}
    pin_all = [(float(p["x"]), float(p["y"]), str(p.get("net")), f"{d}:{p.get('pinNumber')}")
               for c in parts.values() for p in (c.get("pins") or []) if p.get("x") is not None]
    mark_pts = [(float(f["x"]), float(f["y"]), str(f.get("net") or f.get("name")))
                for f in marks if f.get("x") is not None]
    # 桩线段(全部网):共线障碍
    stubs: dict[str, list] = {}
    for nf, fl in mby.items():
        for fx, fy, _k in fl:
            for px, py, pn, pd in pin_all:
                if pn == nf and abs(px - fx) + abs(py - fy) <= 120.0:
                    stubs.setdefault(nf, []).append((px, py, fx, fy))
    print(f"{'网名':14s} {'脚距':>5s}  候选裁定")
    for net in sorted(nets):
        pins = nets[net]
        if len(pins) < 2:
            continue
        ordered = sorted(pins, key=lambda t: (t[2], t[3]))
        a, b = ordered[0], ordered[1]
        fl = mby.get(net, [])
        axes = []
        for d, _p, x, y in ordered[:2]:
            near = sorted([f for f in fl if abs(f[0] - x) + abs(f[1] - y) <= 120.0],
                          key=lambda f: abs(f[0] - x) + abs(f[1] - y))
            axes.append("h" if not near or abs(near[0][0] - x) >= abs(near[0][1] - y) else "v")
        pair = {a[0], b[0]}
        fb = [(d, r) for d, r in bodies.items() if d not in pair]
        fp = [(px, py, pd) for px, py, pn, pd in pin_all if pn != net]
        fm = [(mx, my, mn) for mx, my, mn in mark_pts if mn != net]
        wl = [l for n2, ls in stubs.items() if n2 != net for l in ls]
        dist = abs(a[2] - b[2]) + abs(a[3] - b[3])
        v = diag((a[2], a[3]), (b[2], b[3]), axes[0], axes[1], fb, fp, fm, wl)
        print(f"{net:14s} {dist:5.0f}  ax={axes[0]}/{axes[1]} " + " | ".join(v))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
