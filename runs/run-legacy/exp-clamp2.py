"""v3'' 决定性实验:overlap 分离(b 沿 y 下推 40,下方不够上推)。镜像 controller._clamp_move_for。

用法: uv run python run/exp-clamp2.py <page>
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


def snap5(raw: float) -> int:
    if raw == 0:
        return 0
    n = int(math.ceil(abs(raw) / 5.0) * 5)
    return n if raw > 0 else -n


def main(page: str) -> int:
    for _ in range(4):
        _, out, _ = cli(["sch", "clusters", "--json", "--doc", page])
        rep = json.loads(out or "{}")
        errs = [f for f in rep.get("findings", []) if f.get("level") == "ERROR"]
        if not errs:
            print("clean")
            return 0
        u = rep["sheetUsable"]
        boxes = {c["designator"]: c["box"] for c in rep.get("clusters", []) if c.get("designator")}
        _, out, _ = cli(["sch", "group", "list", "--json", "--doc", page])
        gid_of = {}
        for g in [g for v in json.loads(out or "{}").get("groupsByPage", {}).values() for g in v]:
            for m in g.get("members", []):
                gid_of.setdefault(m.get("designator"), g.get("id"))
        print(f"ERRORs={[(f['type'], f.get('a'), f.get('b')) for f in errs]}")
        acted = False
        for f in errs:
            if f.get("type") != "overlap":
                print(f"  (跳过 {f['type']})")
                continue
            a, b = f.get("a"), f.get("b")
            ba, bb = boxes.get(a), boxes.get(b)
            if not ba or not bb or b not in gid_of:
                continue
            down = snap5(ba["minY"] - 40 - bb["maxY"])
            if bb["maxY"] + down < u["minY"]:
                up = snap5(ba["maxY"] + 40 - bb["minY"])
                if bb["maxY"] + up > u["maxY"]:
                    print(f"  !! {a}/{b} 上下都推不开")
                    continue
                dy = up
            else:
                dy = down
            gid = gid_of[b]
            print(f"  {b} gid={gid} dy={dy}")
            rc, out2, err = cli(["sch", "group-move", "--group", gid, "--dx", "0", "--dy", str(dy), "--doc", page])
            print(f"    rc={rc}: {(out2 or err).strip().splitlines()[-1]}")
            acted = True
            break
        if not acted:
            print("无动作")
            return 1
    _, out, _ = cli(["sch", "clusters", "--json", "--doc", page])
    errs = [f for f in json.loads(out or "{}").get("findings", []) if f.get("level") == "ERROR"]
    print(f"终态 ERROR={len(errs)} {[(f['type'], f.get('a'), f.get('b')) for f in errs]}")
    return 0 if not errs else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
