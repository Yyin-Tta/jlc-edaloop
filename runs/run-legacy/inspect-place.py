"""连接器审计:r1 时段(13:0x-13:2x)的器件摆放类 action + payload。"""
import json

path = r"C:\Users\十三州府\.easyeda-agent\audit\2026-08-21.jsonl"
for line in open(path, encoding="utf-8"):
    try:
        e = json.loads(line)
    except ValueError:
        continue
    a = e.get("action", "")
    if "place" not in a and "Place" not in a:
        continue
    ts = e.get("ts", "")
    hhmm = ts[11:16]
    if not ("12:55" <= hhmm <= "13:25"):
        continue
    p = e.get("payload", {})
    keep = {k: p[k] for k in ("x", "y", "designator", "name", "uuid") if k in p}
    print(hhmm, ts[17:23], a, json.dumps(keep, ensure_ascii=False)[:180])
