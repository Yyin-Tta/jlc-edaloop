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

    # 增量修复轮(2026-09-15):修复轮只重放脏页动作,冻结页内容在上一个
    # 全量轮的审计里——重放范围从「最后一个全量轮」起拼接其后所有修复轮,
    # 否则只重放末个修复轮会得到一份缺冻结页的残缺图。轮型以 repair-round
    # 事件为唯一权威(page-clear 的 mode 字段在 clear_failed 早退轮也会发,
    # 双源判型易漂)。无修复轮(旧审计/未开启)时 base==last,行为与旧版
    # 逐字节一致。
    repair_rounds = {e.get("round_no") for e in events if e.get("kind") == "repair-round"}
    last_round = max((e.get("round_no") or 0) for e in events)
    base_round = max(
        (e.get("round_no") or 0) for e in actions
        if (e.get("round_no") or 0) not in repair_rounds
    ) if repair_rounds else last_round
    final = [e for e in actions if base_round <= (e.get("round_no") or 0) <= last_round]

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
            if e.get("mode") == "repair":
                # 修复轮清页只清脏页:重放同样按事件里的 dirty 页逐页清,
                # clear_all_pages 会把全量轮刚放好的冻结页一并抹掉。
                for p in e.get("dirty") or []:
                    if not dry_run:
                        adapter.run(["sch", "clear", "--doc", p])
            elif not dry_run:
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
        "base_round": base_round,
        "replayed": replayed,
        "errors": errors,
        "gate_verdict": (gate_report or {}).get("verdict", "not-run"),
    }
