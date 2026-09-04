"""run 进度速览:按时间序列出关键事件。用法: uv run python run/inspect-fail.py <audit路径>"""
import json
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "runs/run-c9f3f63d78e9/audit.jsonl"
for line in open(path, encoding="utf-8"):
    e = json.loads(line)
    k = e.get("kind", "?")
    if k in ("finding", "note"):
        continue
    extra = ""
    if k == "block-apply":
        extra = f" {e['instance']}@{e.get('page')} {e['status']}" + (" RETRY" if e.get("retry") else "")
    elif k == "page-clear-doc":
        extra = f" p={e.get('page')} ok={e.get('ok')}"
    elif k == "gate":
        extra = f" p={e.get('page')} {e.get('verdict')}"
    elif "page" in e:
        extra = f" p={e.get('page')}"
    print(f"r{e.get('round_no', '?')} {e.get('ts', '')[11:19]} {k}{extra}")
