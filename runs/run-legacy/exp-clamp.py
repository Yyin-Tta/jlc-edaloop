"""v3' 决定性实验:out-of-sheet 点名件按 clusters sheetUsable 钳回(group-move)。

用法: uv run python run/exp-clamp.py <page>   # 如 P1
"""
from __future__ import annotations

import json
import math
import subprocess
import sys


def cli(args: list[str]) -> tuple[int, str, str]:
    r = subprocess.run(
        ["uv", "run", "easyeda", *args], capture_output=True, text=True, encoding="utf-8"
    )
    return r.returncode, r.stdout or "", r.stderr or ""


def clamp_delta(lo, hi, blo, bhi) -> int:
    raw = 0.0
    if hi > bhi:
        raw = bhi - hi
    elif lo < blo:
        raw = blo - lo
    if raw == 0:
        return 0
    n = int(math.ceil(abs(raw) / 5.0) * 5)
    return n if raw > 0 else -n


def main(page: str) -> int:
    _, out, _ = cli(["sch", "clusters", "--json", "--doc", page])
    rep = json.loads(out or "{}")
    u = rep.get("sheetUsable") or {}
    errs = [f for f in rep.get("findings", []) if f.get("level") == "ERROR"]
    print(f"sheetUsable={u}")
    print(f"ERRORs={[(f['type'], f.get('a'), f.get('b')) for f in errs]}")
    boxes = {c["designator"]: c["box"] for c in rep.get("clusters", []) if c.get("designator")}
    _, out, _ = cli(["sch", "group", "list", "--json", "--doc", page])
    gid_of = {}
    for g in [g for v in json.loads(out or "{}").get("groupsByPage", {}).values() for g in v]:
        for m in g.get("members", []):
            gid_of.setdefault(m.get("designator"), g.get("id"))
    for f in errs:
        if f.get("type") != "out-of-sheet":
            continue
        d = f.get("a")
        b = boxes.get(d) or {}
        gid = gid_of.get(d)
        if not b or not gid:
            print(f"  !! {d}: 无 box 或无组(gid={gid})")
            continue
        dx = clamp_delta(b["minX"], b["maxX"], u["minX"], u["maxX"])
        dy = clamp_delta(b["minY"], b["maxY"], u["minY"], u["maxY"])
        print(f"  {d} box=({b['minX']:.0f},{b['minY']:.0f})-({b['maxX']:.0f},{b['maxY']:.0f}) gid={gid} → dx={dx} dy={dy}")
        if dx or dy:
            rc, out, err = cli(["sch", "group-move", "--group", gid, "--dx", str(dx), "--dy", str(dy), "--doc", page])
            print(f"    group-move rc={rc}: {(out or err).strip().splitlines()[-1]}")
    _, out, _ = cli(["sch", "clusters", "--json", "--doc", page])
    rep2 = json.loads(out or "{}")
    errs2 = [f for f in rep2.get("findings", []) if f.get("level") == "ERROR"]
    print(f"复查: ERROR={len(errs2)} {[(f['type'], f.get('a'), f.get('b')) for f in errs2]}")
    return 0 if not errs2 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
