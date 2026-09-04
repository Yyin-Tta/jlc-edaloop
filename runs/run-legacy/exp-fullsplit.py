"""v3 决定性实验:整页拆散成单件组 → group-arrange → clusters 复查。

用法: uv run python run/exp-fullsplit.py <page> [gap]   # 如 P1 80
"""
from __future__ import annotations

import json
import subprocess
import sys


def cli(args: list[str]) -> tuple[int, str]:
    r = subprocess.run(
        ["uv", "run", "easyeda", *args], capture_output=True, text=True, encoding="utf-8"
    )
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main(page: str, gap: str) -> int:
    rc, out = cli(["sch", "group", "list", "--json", "--doc", page])
    gs = [g for v in json.loads(out or "{}").get("groupsByPage", {}).values() for g in v]
    for g in gs:
        members = [m["designator"] for m in g.get("members", []) if m.get("designator")]
        print(f"ungroup {g['id']}: {members}")
        cli(["sch", "group", "ungroup", "--group", g["id"], "--doc", page])
        for d in members:
            cli(["sch", "group", "create", "--members", d, "--doc", page])
    rc, out = cli(["sch", "group-arrange", "--annotate=false", "--gap", gap, "--doc", page])
    tail = out.strip().splitlines()[-3:]
    print(f"arrange gap={gap} rc={rc}: {' | '.join(tail)}")
    rc, out = cli(["sch", "clusters", "--json", "--doc", page])
    findings = json.loads(out or "{}").get("findings", [])
    errs = [f for f in findings if f.get("level") == "ERROR"]
    print(f"clusters: ERROR={len(errs)} all={[(f['type'], f.get('a'), f.get('b')) for f in findings]}")
    return 0 if not errs else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "80"))
