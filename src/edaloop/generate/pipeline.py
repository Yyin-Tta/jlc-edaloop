from __future__ import annotations

import json
from pathlib import Path

from edaloop.generate.adapter import EasyedaAdapter
from edaloop.generate.audit import AuditLog
from edaloop.generate.compile import compile_actions
from edaloop.generate.models import BlockPlan
from edaloop.generate.plan import make_plan
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
        candidates = store.retrieve(ir.query_text() or md_text, top_k=top_k)
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
