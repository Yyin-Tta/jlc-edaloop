"""E2E run3 验收核查(§4.5 续作 3):从审计 jsonl + run 目录机械核验四要件。

用法: uv run python run/verify-e2e-b2r3.py <run_dir>   # 如 run/run-xxxx
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

WINDOW = "0b6851eb-2831-469a-bc1b-6f1c8c45c67a"


def main(run_dir: str) -> int:
    audit = Path(run_dir) / "audit.jsonl"
    events = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines()]
    print(f"== {run_dir}: {len(events)} events ==")

    # 1) 终态与轮次
    last_round = max((e.get("round_no", 0) for e in events if e.get("kind") == "round-plan"), default=0)
    print(f"rounds={last_round}")

    # 2) 清页保真事件(新通道)
    pcd = [e for e in events if e.get("kind") == "page-clear-doc"]
    bad = [e for e in pcd if not e.get("ok")]
    print(f"page-clear-doc: {len(pcd)} events, not-ok={len(bad)}")
    for e in bad:
        print(f"  !! p={e.get('page')} attempt={e.get('attempt')} remaining={e.get('remaining')} survivors={e.get('survivors')} warn={e.get('warnings')}")

    # 3) block-apply 全 applied?
    applies = [e for e in events if e.get("kind") == "block-apply"]
    fails = [e for e in applies if e.get("status") != "applied"]
    print(f"block-apply: {len(applies)}, non-applied={len(fails)}")
    for e in fails:
        print(f"  !! {e.get('instance')}@{e.get('page')} {e.get('status')}: {str(e.get('failure'))[:120]}")

    # 4) 逐页 gate verdict
    gates = [e for e in events if e.get("kind") == "gate"]
    by_page: dict[str, str] = {}
    for g in gates:
        p = g.get("page", "?")
        by_page[p] = g.get("verdict", "?")  # 同页取最后一次
    print(f"gate per page: {by_page}")

    # 5) zone-plan 五项(+ 修复级 zone-arrange 与 zone-draw 结果)
    zp = [e for e in events if e.get("kind") == "zone-plan"]
    for z in zp:
        v = z.get("validation", {})
        nonzero = {k: n for k, n in v.items() if n}
        print(f"zone-plan p={z.get('page')}: {'ALL-ZERO' if not nonzero else nonzero}")
    for e in events:
        if e.get("kind") == "zone-arrange":
            print(f"zone-arrange p={e.get('page')}: rc={e.get('rc')} out={str(e.get('out'))[-120:]}")
        if e.get("kind") == "zone-draw":
            print(f"zone-draw p={e.get('page')}: rc={e.get('rc')}")

    # 5b) P4-b3 布局收口:probe 页必须收口到 remaining=0;titleblock 逐页落了没
    probes = {e.get("page") for e in events if e.get("kind") == "arrange-probe"}
    results = {e.get("page"): e.get("remaining") for e in events if e.get("kind") == "arrange-result"}
    applies_arr = [e for e in events if e.get("kind") == "arrange-apply"]
    for e in applies_arr:
        print(f"arrange-apply p={e.get('page')} gap={e.get('gap')} rc={e.get('rc')}")
    arrange_bad = []
    for p in sorted(probes):
        rem = results.get(p)
        print(f"arrange-result p={p}: remaining={rem}")
        if rem:
            arrange_bad.append(p)
    clamps = [e for e in events if e.get("kind") == "arrange-clamp"]
    for e in clamps:
        print(
            f"arrange-clamp p={e.get('page')} cause={e.get('cause')} {e.get('designator')}"
            f" d=({e.get('dx')},{e.get('dy')}) rc={e.get('rc')}"
        )
    tb = [e for e in events if e.get("kind") == "titleblock"]
    for e in tb:
        print(f"titleblock p={e.get('page')}: key={e.get('key')} rc={e.get('rc')} show_rc={e.get('show_rc')}")

    # 6) 交付物
    run_path = Path(run_dir)
    svg = sorted(run_path.glob("delivery-P*.svg"))
    print(f"delivery pages: {[s.name for s in svg]}")
    for extra in ("delivery.net.json", "delivery.bom.json", "delivery.sizing.txt", "delivery.review.txt"):
        print(f"  {extra}: {'OK' if (run_path / extra).exists() else '-'}")

    ok = (
        not bad
        and not fails
        and all(v == "pass" for v in by_page.values())
        and by_page
        and not arrange_bad
        and len(svg) >= 2
    )
    print(f"== VERDICT: {'ACCEPT' if ok else 'CHECK-ABOVE'} ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
