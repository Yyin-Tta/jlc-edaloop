import json
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "runs/run-c37d8ed23a66/audit.jsonl"
for line in open(path, encoding="utf-8"):
    e = json.loads(line)
    k = e.get("kind")
    if k == "block-apply":
        a = e["args"]
        at = a[a.index("--at") + 1] if "--at" in a else "-"
        sp = a[a.index("--spacing") + 1] if "--spacing" in a else "-"
        print(f"r{e['round_no']} {e['instance']}@{e.get('page')} {e['status']} blk={a[2]} at={at} sp={sp} fail={str(e.get('failure'))[:80]}")
    elif k in ("sch-place", "zone-draw"):
        print(f"r{e.get('round_no')} {k} {e.get('instance', '')}{e.get('designator', '')}@{e.get('page')} ok={e.get('ok', e.get('rc'))}")
