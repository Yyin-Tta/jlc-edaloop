"""从 ~/.easyeda-agent/audit/2026-08-21.jsonl 捞 run3b 失败窗口的原始 action 记录。

用法: uv run python run/inspect-audit.py <HH:MM:SS起> <HH:MM:SS止> [action过滤]
"""
import json
import sys

t0, t1 = sys.argv[1], sys.argv[2]
want = sys.argv[3] if len(sys.argv) > 3 else ""
path = r"C:\Users\十三州府\.easyeda-agent\audit\2026-08-21.jsonl"

for line in open(path, encoding="utf-8"):
    if '"2026-08-21T' + t0[:2] + ":" not in line:
        continue
    e = json.loads(line)
    ts = e.get("ts", "")[11:19]
    if not (t0 <= ts <= t1):
        continue
    act = e.get("action", "")
    if want and want not in act:
        continue
    dur = e.get("durationMs")
    ok = e.get("ok")
    res = e.get("result") or {}
    n = ""
    if isinstance(res, dict) and "components" in res:
        comps = res["components"]
        n = f" comps={len(comps)}"
        # 概览:类型/位号/坐标,查重复
        seen = {}
        for c in comps:
            if not isinstance(c, dict):
                continue
            lab = c.get("designator") or c.get("label") or "?"
            ct = c.get("componentType", "?")
            bb = c.get("bbox") or {}
            key = (lab, ct)
            seen[key] = seen.get(key, 0) + 1
            if want or "components.list" in act:
                print(f"    {ct:12s} {lab:8s} bbox={bb.get('minX')},{bb.get('minY')}-{bb.get('maxX')},{bb.get('maxY')} pins={len(c.get('pins') or [])}")
        dups = {k: v for k, v in seen.items() if v > 1}
        if dups:
            print(f"    !! 重复条目: {dups}")
    print(f"{ts} {act} ok={ok} {dur}ms{n}")
