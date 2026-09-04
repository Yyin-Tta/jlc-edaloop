"""导出 run4 r1 的全部落图动作(block-apply 含 binds + sch-place)供复刻。"""
import json

for line in open("runs/run-c9f3f63d78e9/audit.jsonl", encoding="utf-8"):
    e = json.loads(line)
    if e.get("round_no") != 1:
        continue
    if e.get("kind") == "block-apply":
        print(json.dumps(e["args"]))
    elif e.get("kind") == "sch-place":
        print(f"PLACE {json.dumps(e.get('args', e.get('argv', [])))}")
