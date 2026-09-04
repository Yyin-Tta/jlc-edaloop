import json
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "runs/run-c37d8ed23a66/audit.jsonl"
for line in open(path, encoding="utf-8"):
    e = json.loads(line)
    if e.get("kind") != "gate":
        continue
    rep = e.get("report") or {}
    stages = rep.get("stages") or []
    print(f"--- r{e['round_no']} gate@{e.get('page')} verdict={e.get('verdict')}")
    for st in stages if isinstance(stages, list) else []:
        if not isinstance(st, dict):
            continue
        name = st.get("stage") or st.get("name")
        detail = st.get("detail")
        if not detail:
            continue
        d = detail if isinstance(detail, dict) else {"raw": str(detail)}
        findings = d.get("findings") or d.get("items") or d.get("overlaps") or []
        print(f"  stage={name} findings={len(findings) if isinstance(findings, list) else findings}")
        for f in (findings if isinstance(findings, list) else [])[:8]:
            print(f"    {json.dumps(f, ensure_ascii=False)[:220]}")
