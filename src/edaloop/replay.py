from __future__ import annotations

import json
from pathlib import Path

from edaloop.generate.adapter import EasyedaAdapter


class ReplayError(Exception):
    pass


def replay_run(
    audit_dir: str,
    *,
    run_json=None,
    dry_run: bool = False,
) -> dict:
    """按审计日志重放一次 run 的落图动作序列。

    - 只重放确定性的编辑类事件(block-apply/sch-place/sch-autoconnect/lib-search/sch-gate/page-clear);
    - LLM 产物(plan/IR)不重算,直接复用审计中记录的最终轮 args;
    - run_json: 注入适配器(测试用);默认真实 EasyedaAdapter。
    """
    audit_path = Path(audit_dir) / "audit.jsonl"
    if not audit_path.exists():
        raise ReplayError(f"审计日志不存在: {audit_path}")

    events = [json.loads(l) for l in audit_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    replayable = {"page-clear", "lib-search", "sch-place", "sch-autoconnect", "block-apply", "gate"}
    actions = [e for e in events if e.get("kind") in replayable]
    if not actions:
        raise ReplayError("审计中没有可重放的动作")

    last_round = max((e.get("round_no") or 0) for e in events)
    final = [e for e in actions if (e.get("round_no") or 0) == last_round]

    adapter = run_json or (None if dry_run else EasyedaAdapter())
    if not dry_run:
        adapter.run(["sch", "pages"])

    replayed = 0
    errors: list[str] = []
    gate_report = None
    for e in final:
        args = e.get("args") or []
        kind = e.get("kind")
        if kind == "page-clear":
            if not dry_run:
                adapter.clear_all_pages()
            replayed += 1
            continue
        if not args:
            continue
        if dry_run:
            replayed += 1
            continue
        try:
            if kind == "gate":
                gate_report = adapter.run_json(args)
            elif kind == "sch-autoconnect":
                adapter.run(args)
            elif kind == "lib-search":
                adapter.run(args)
            else:
                adapter.run_json(args)
        except Exception as ex:
            errors.append(f"{kind} {e.get('instance', '')}: {str(ex)[:150]}")
        replayed += 1

    return {
        "audit_dir": audit_dir,
        "final_round": last_round,
        "replayed": replayed,
        "errors": errors,
        "gate_verdict": (gate_report or {}).get("verdict", "not-run"),
    }
