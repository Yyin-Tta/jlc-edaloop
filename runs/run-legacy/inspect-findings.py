"""round-validate blocking 全量明细。用法: uv run python run/inspect-findings.py <audit路径>"""
import json
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "runs/run-c9f3f63d78e9/audit.jsonl"
for line in open(path, encoding="utf-8"):
    e = json.loads(line)
    if e.get("kind") != "round-validate":
        continue
    print(f"--- r{e.get('round_no')} gate={e.get('gate')}")
    for f in e.get("blocking", []):
        print(
            f"  {f.get('code')} ref={f.get('where', {}).get('ref')} "
            f"ev={f.get('evidence')} fix={f.get('suggested_fix_class')}"
        )
    for f in e.get("weak", [])[:10]:
        print(f"  [weak] {f.get('code')} ev={str(f.get('evidence'))[:100]}")
