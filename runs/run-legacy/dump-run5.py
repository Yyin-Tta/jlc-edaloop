"""run5 审计摘要:arrange/gate 事件流(GBK 控制台安全输出)。"""
import json
import sys
from pathlib import Path

audit = Path(sys.argv[1] if len(sys.argv) > 1 else r"E:\jlc-edaloop\runs\run-6ba4e2535cb1\audit.jsonl")
events = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines()]
print("total", len(events))
for e in events:
    k = e.get("kind")
    r = e.get("round_no")
    p = e.get("page", "")
    if k == "arrange-probe":
        print(f"r{r} PROBE {p}", [(f.get("type"), f.get("a"), f.get("b")) for f in e.get("errors", [])])
    elif k == "arrange-apply":
        err = (e.get("error") or "")[:100].replace("\n", " ")
        out = (e.get("out") or "")[:60].replace("\n", " ")
        print(f"r{r} APPLY {p} gap={e.get('gap')} rc={e.get('rc')} err={err} out={out}")
    elif k == "arrange-result":
        print(f"r{r} RESULT {p} remaining={e.get('remaining')}")
    elif k == "arrange-shatter":
        print(f"r{r} SHATTER {p} group={e.get('group')} members={e.get('members')}")
    elif k == "gate":
        print(f"r{r} GATE {p} {e.get('verdict')}")
    elif k in ("loop-done", "loop-halt"):
        print(k, {kk: e.get(kk) for kk in ("status", "rounds", "reason")})
    elif k == "round-plan":
        print(f"r{r} PLAN blocks={e.get('blocks')}")
    elif k == "round-validate":
        codes = [b.get("evidence") for b in e.get("blocking", [])]
        print(f"r{r} VALIDATE blocking={codes}")
