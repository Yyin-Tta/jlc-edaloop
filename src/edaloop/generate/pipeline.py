from __future__ import annotations

import json
from pathlib import Path

from edaloop.generate.adapter import EasyedaAdapter
from edaloop.generate.audit import AuditLog
from edaloop.generate.compile import compile_actions
from edaloop.generate.models import BlockPlan
from edaloop.generate.plan import make_plan
from edaloop.intent.acceptance import parse_acceptance
from edaloop.intent.ir import DesignIR
from edaloop.intent.parse import requirement_to_ir
from edaloop.knowledge.models import BlockRecord
from edaloop.knowledge.store import KnowledgeStore
from edaloop.llm.base import LLMProvider
from edaloop.llm.openai_compat import get_embedder, get_llm, get_reranker


def load_catalog(seeds_path: str | Path = "seeds/blocks.jsonl") -> dict[str, BlockRecord]:
    blocks = [
        BlockRecord.model_validate(json.loads(line))
        for line in Path(seeds_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return {b.block_id: b for b in blocks}


def make_retriever(db_path: str = "runs/knowledge.db", top_k: int = 12, ir=None):
    # P4-6③:ir 透传 → 案例第五通道开;生产路径(stage_run/stage_plan)传 ir,
    # 评测路径(evals_w1/evals_refine)只喂 query_text → 通道结构性消融。
    def retrieve(query: str):
        store = KnowledgeStore(db_path, get_embedder(), get_reranker())
        try:
            return store.retrieve(query or "power mcu interface", top_k=top_k, ir=ir)
        finally:
            store.close()

    return retrieve


def _maybe_record_case(ir, result, *, source: str, dry_run: bool, db_path: str, audit: AuditLog) -> None:
    """P4-6③ 案例 PASS 回写。三护栏:①eval 源不写(req-*/evals 路径直接拒);
    ②origin=run:<ir.id> 溯源;③hash 去重(store.record_case 内 sha256)。
    """
    if dry_run or result.status != "PASS" or result.final_plan is None:
        return
    src = Path(source)
    if src.name.lower().startswith("req-") or "evals" in source.lower().replace("\\", "/"):
        return
    block_ids = sorted({b.block_id for b in result.final_plan.blocks})
    if not block_ids:
        return
    from datetime import datetime, timezone

    from edaloop.knowledge.models import CaseRecord
    from edaloop.knowledge.store import _case_digest_of

    try:
        store = KnowledgeStore(db_path, get_embedder(), get_reranker())
        try:
            case = CaseRecord(
                case_id=f"case-{ir.id}",
                name=(ir.functions[0].name if ir.functions else src.stem)[:60],
                origin=f"run:{ir.id}",
                digest=_case_digest_of(ir),
                block_ids=block_ids,
                created=datetime.now(timezone.utc).isoformat(),
            )
            inserted = store.record_case(case)
            audit.event("case-writeback", case_id=case.case_id, inserted=inserted, blocks=case.block_ids)
        finally:
            store.close()
    except Exception as e:  # 回写是增益通道,失败不拖垮 PASS 交付
        audit.event("case-writeback-error", error=str(e)[:200])


def _parse_ir_with_retry(md_text: str, llm, source: str, attempts: int = 3) -> DesignIR:
    from edaloop.intent.parse import IRParseError

    last: Exception | None = None
    for _ in range(attempts):
        try:
            return requirement_to_ir(md_text, llm, source=source)
        except IRParseError as e:
            last = e
    raise last if last else RuntimeError("ir parse failed")


def stage_run(
    md_text: str,
    *,
    source: str,
    max_rounds: int = 5,
    dry_run: bool = False,
    db_path: str = "runs/knowledge.db",
    seeds_path: str | Path = "seeds/blocks.jsonl",
    answers: dict[str, str] | None = None,
    ir_path: str | None = None,
    retry_queries: list[str] | None = None,
):
    from edaloop.generate.adapter import EasyedaAdapter
    from edaloop.loop.controller import LoopController

    llm = get_llm()
    if ir_path:
        ir = DesignIR.model_validate_json(Path(ir_path).read_text(encoding="utf-8"))
    else:
        ir = _parse_ir_with_retry(md_text, llm, source=source)
    answer_context = ""
    if answers:
        applied = ir.apply_answers(answers)
        if applied:
            answer_context = "\n\n用户已确认的决策(优先级高于你的默认选择,不要再问):\n" + "\n".join(
                f"- [{qid}] {ans}" for qid, ans in answers.items() if ans
            )
    retriever = make_retriever(db_path, ir=ir)
    audit = AuditLog(f"runs/run-{ir.id}")
    audit.event(
        "ir",
        source=source,
        ir=json.loads(ir.model_dump_json()),
        revision=ir.revision,
        answers_applied=len(answers) if answers else 0,
        from_refine=bool(ir_path),
    )
    controller = LoopController(
        ir,
        load_catalog(seeds_path),
        retriever,
        llm,
        EasyedaAdapter(),
        audit,
        max_rounds=max_rounds,
        dry_run=dry_run,
        answer_context=answer_context,
        retry_queries=retry_queries or [],
        acceptance_items=parse_acceptance(md_text),  # P4-5①:「## 期望指标」段不再丢弃
    )
    result = controller.run()
    _maybe_record_case(ir, result, source=source, dry_run=dry_run, db_path=db_path, audit=audit)
    delivery = controller.deliver(result)
    audit.save_json(
        "loop-result.json",
        {
            "status": result.status,
            "rounds": [r.__dict__ for r in result.rounds],
            "delivery": delivery,
        },
    )
    return ir, result


def stage_plan(
    md_text: str,
    *,
    source: str,
    llm: LLMProvider | None = None,
    db_path: str = "runs/knowledge.db",
    top_k: int = 8,
) -> tuple[DesignIR, BlockPlan]:
    llm = llm or get_llm()
    ir = requirement_to_ir(md_text, llm, source=source)
    store = KnowledgeStore(db_path, get_embedder(), get_reranker())
    try:
        candidates = store.retrieve(ir.query_text() or md_text, top_k=top_k, ir=ir)
    finally:
        store.close()
    plan = make_plan(ir, candidates, llm)
    return ir, plan


def stage_apply(
    plan: BlockPlan,
    *,
    catalog: dict[str, BlockRecord] | None = None,
    adapter: EasyedaAdapter | None = None,
    audit: AuditLog | None = None,
) -> dict:
    catalog = catalog or load_catalog()
    adapter = adapter or EasyedaAdapter()
    audit = audit or AuditLog(f"runs/{plan.id}")
    actions = compile_actions(plan, catalog)
    audit.event("plan-loaded", blocks=len(plan.blocks), confidence=plan.confidence)
    for act in actions:
        audit.event("action", action_kind=act.kind, args=act.args, desc=act.desc)
    adapter.check_version()
    adapter.daemon_health()
    results = []
    for act in actions:
        if act.kind == "sch-gate":
            report = adapter.run_json(act.args)
            verdict = report.get("verdict", "unknown")
            results.append({"kind": "gate", "verdict": verdict, "report": report})
            audit.event("gate", verdict=verdict)
            continue
        manifest = adapter.run_json(act.args)
        status = manifest.get("ok") or manifest.get("status") or "unknown"
        if str(status).startswith("failed-partial"):
            survivors = manifest.get("rollback", {}).get("survivedPrimitiveIds", [])
            if survivors:
                adapter.delete_primitives(survivors)
                audit.event("cleanup", instance=act.block_instance, deleted=survivors)
            manifest = adapter.run_json(act.args)
            status = manifest.get("ok") or manifest.get("status") or "unknown"
            audit.event("block-apply-retry", instance=act.block_instance, status=status)
        results.append(
            {"kind": "block-apply", "instance": act.block_instance, "status": status, "manifest": manifest}
        )
        audit.event("block-apply", instance=act.block_instance, status=status)
    audit.save_json("results.json", results)
    gate = next((r for r in results if r["kind"] == "gate"), None)
    failures = [
        r for r in results if r["kind"] == "block-apply" and str(r.get("status", "")).startswith("failed")
    ]
    return {
        "plan_id": plan.id,
        "results": results,
        "gate_verdict": gate["verdict"] if gate else "not-run",
        "apply_failures": failures,
        "audit_dir": str(audit.dir),
    }
