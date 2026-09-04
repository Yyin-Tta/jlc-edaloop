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
from edaloop.knowledge.models import RetrievedBlock
from edaloop.knowledge.store import KnowledgeStore
from edaloop.llm.base import LLMProvider
from edaloop.llm.openai_compat import get_embedder, get_llm, get_reranker


_APPLY_FAILURE_WORDS = {
    "fail", "failed", "failure", "error", "blocked", "invalid", "timeout",
    "timed-out", "cancelled", "canceled", "false", "0",
}


def _apply_status_is_failure(status: object, manifest: dict) -> bool:
    """Recognize failed/negative apply responses without truthiness traps."""

    if status is None:
        return True
    if isinstance(status, bool):
        return not status
    if isinstance(status, (int, float)) and not isinstance(status, bool):
        return status == 0
    token = str(status or "").strip().casefold().replace(" ", "-")
    if token in _APPLY_FAILURE_WORDS or token.startswith("failed-"):
        return True
    if token in {"", "unknown", "pending", "not-run", "not_run", "none", "null"}:
        return True
    # Contradictory aliases (for example ``ok=true,status=failed``) are not
    # usable evidence.  Treat the explicit negative field as a failure even
    # when status extraction selected the other alias.
    for key in ("ok", "success", "passed", "status", "state"):
        if key not in manifest:
            continue
        raw = manifest.get(key)
        raw_token = str(raw or "").strip().casefold().replace(" ", "-")
        if raw is False or raw_token in _APPLY_FAILURE_WORDS or raw_token.startswith("failed-"):
            return True
    return any(
        key in manifest and manifest.get(key) not in (None, "", True)
        for key in ("error", "exception", "failure")
    )


def _apply_status(manifest: dict) -> object:
    """Select a status while making contradictory negative aliases win."""

    values = [
        manifest[key]
        for key in ("ok", "success", "passed", "status", "state")
        if key in manifest and manifest[key] is not None
    ]
    if not values:
        return "unknown"
    def _negative(value: object) -> bool:
        if isinstance(value, bool):
            return not value
        if isinstance(value, (int, float)):
            return value == 0
        token = str(value).strip().casefold().replace(" ", "-")
        return token in _APPLY_FAILURE_WORDS or token.startswith("failed-")

    for value in values:
        token = str(value).strip().casefold().replace(" ", "-")
        if token.startswith("failed-"):
            return value
    if any(_negative(value) for value in values):
        return "failed"
    return values[0]


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


def _maybe_record_case(ir, result, *, source: str, dry_run: bool, db_path: str,
                       audit: AuditLog, delivery: dict | None = None) -> None:
    """P4-6③ 案例 PASS 回写。三护栏:①eval 源不写(req-*/evals 路径直接拒);
    ②origin=run:<ir.id> 溯源;③hash 去重(store.record_case 内 sha256)。
    """
    if dry_run or result.status != "PASS" or result.final_plan is None:
        return
    # Case writeback is downstream of delivery.  Keep ``delivery`` optional
    # for older direct callers/tests, but the production stage_run path always
    # supplies it and requires an explicit contract PASS.
    if delivery is not None and delivery.get("ok") is not True:
        audit.event("case-writeback-skip", reason="delivery-incomplete",
                    missing=delivery.get("missing", []))
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
    audit_listener=None,
    project: str | None = None,
    window: str | None = None,
    plan_path: str | None = None,
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
    if plan_path:
        # A plan replay is an offline layout regression.  Use the local seed
        # catalog as candidates so embedding/rerank service outages cannot
        # prevent deterministic verification of an already chosen plan.
        seed_catalog = load_catalog(seeds_path)
        retriever = lambda _query: [
            RetrievedBlock.model_validate({
                **record.model_dump(), "score": 0.0,
                "channels": ["plan-replay"], "rank": index,
            })
            for index, record in enumerate(seed_catalog.values())
        ]
    else:
        retriever = make_retriever(db_path, ir=ir)
    # audit_listener:UI 事件总线(见 generate/audit.py),None = 既有 CLI/eval 用法
    audit = AuditLog(f"runs/run-{ir.id}", listener=audit_listener)
    audit.event(
        "ir",
        source=source,
        ir=json.loads(ir.model_dump_json()),
        revision=ir.revision,
        answers_applied=len(answers) if answers else 0,
        from_refine=bool(ir_path),
    )
    fixed_plan = None
    if plan_path:
        audit.event("plan-replay-start", path=str(plan_path))
        raw_plan = Path(plan_path).read_text(encoding="utf-8-sig")
        payload = None
        try:
            payload = json.loads(raw_plan)
        except json.JSONDecodeError:
            for line in raw_plan.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("kind") == "round-plan" and event.get("plan"):
                    payload = event["plan"]
        if isinstance(payload, dict) and isinstance(payload.get("plan"), dict):
            payload = payload["plan"]
        if not isinstance(payload, dict):
            raise ValueError(f"无法从 --plan 读取 BlockPlan: {plan_path}")
        for block in payload.get("blocks", []):
            if block.get("block_id") == "battery-dw01-protection":
                bindings = dict(block.get("pins_binding") or {})
                bindings.pop("4", None)
                block["pins_binding"] = bindings
                block["no_connect"] = sorted(set(block.get("no_connect") or []) | {"4"})
        fixed_plan = BlockPlan.model_validate(payload)
        audit.event("plan-replay-source", path=str(plan_path), plan_id=fixed_plan.id)
    audit.event("controller-init", fixed_plan=bool(fixed_plan), project=project or "", window=window or "")
    controller = LoopController(
        ir,
        load_catalog(seeds_path),
        retriever,
        llm,
        EasyedaAdapter(project=project, window=window),
        audit,
        max_rounds=max_rounds,
        dry_run=dry_run,
        answer_context=answer_context,
        retry_queries=retry_queries or [],
        acceptance_items=parse_acceptance(md_text),  # P4-5①:「## 期望指标」段不再丢弃
        fixed_plan=fixed_plan,
    )
    result = controller.run()
    delivery = controller.deliver(result)
    _maybe_record_case(ir, result, source=source, dry_run=dry_run, db_path=db_path,
                       audit=audit, delivery=delivery)
    audit.save_json(
        "loop-result.json",
        {
            "status": result.status,
            "review_required": getattr(result, "review_required", False),
            "failure_class": getattr(result, "failure_class", ""),
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
    """Apply a precompiled plan through the low-level command path.

    This entry point intentionally remains a thin M3b/evaluation helper.  It
    does *not* run the ``LoopController`` closeout sequence (terminal layout
    snapshot, per-page gate contract, and delivery artifact contract).  A
    connector ``verdict=pass`` therefore cannot be promoted to an engineering
    PASS here.  The raw connector verdict is retained for diagnostics while
    the public summary is fail-closed as ``unverified``; callers that need a
    deliverable result must use :func:`stage_run`.
    """
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
            gate_args = list(act.args)
            # Ask the connector for its strict checker mode when available.
            # This improves the diagnostic report, but does not widen the
            # trust boundary: stage_apply still remains unverified below.
            if "--strict" not in gate_args:
                gate_args.append("--strict")
            gate_rc = 0
            gate_stderr = ""
            try:
                run_with_rc = getattr(adapter, "run_json_with_rc", None)
                if callable(run_with_rc):
                    gate_rc, report, gate_stderr = run_with_rc(gate_args)
                else:
                    report = adapter.run_json(gate_args)
            except Exception:
                # Preserve the historical exception behavior for transport or
                # JSON failures; a missing report must never look like PASS.
                raise
            if not isinstance(report, dict):
                report = {"verdict": "unknown", "raw_report": report}
            verdict = report.get("verdict", "unknown")
            results.append(
                {
                    "kind": "gate",
                    "verdict": verdict,
                    "report": report,
                    "rc": gate_rc,
                    "stderr": str(gate_stderr or "")[-500:],
                    "args": gate_args,
                }
            )
            audit.event("gate", verdict=verdict, rc=gate_rc, stderr=str(gate_stderr or "")[-500:])
            continue
        manifest = adapter.run_json(act.args)
        if not isinstance(manifest, dict):
            # A JSON array/scalar is a connector contract violation.  Keep it
            # in the audit without allowing ``.get`` to crash or accidentally
            # turn the action into a successful result.
            manifest = {
                "status": "unknown",
                "error": "block-apply response is not an object",
                "raw_manifest": manifest,
            }
        status = _apply_status(manifest)
        if str(status).startswith("failed-partial"):
            rollback = manifest.get("rollback")
            survivors = rollback.get("survivedPrimitiveIds", []) if isinstance(rollback, dict) else []
            if not isinstance(survivors, (list, tuple)):
                survivors = []
            if survivors:
                adapter.delete_primitives(survivors)
                audit.event("cleanup", instance=act.block_instance, deleted=survivors)
            # block-apply is not idempotent.  Reissuing the same command after
            # a partial rollback can leave a second copy when the rollback or
            # response was stale.  The full controller has readback/adoption
            # logic; this low-level path must stop after cleaning only the
            # primitive ids the connector explicitly reported.
            audit.event(
                "block-apply-no-retry",
                instance=act.block_instance,
                status=status,
                reason="non-idempotent command requires controller readback",
            )
        results.append(
            {"kind": "block-apply", "instance": act.block_instance, "status": status, "manifest": manifest}
        )
        audit.event("block-apply", instance=act.block_instance, status=status)
    # ``stage_apply`` predates the strict LoopController path and still lacks
    # terminal geometry/readback and delivery checks.  Keep the connector's
    # report visible, but never expose its nominal PASS as this function's
    # engineering verdict.  This is deliberately unconditional (including
    # injected/fake adapters): a fake must opt into the full controller path,
    # not silently widen the low-level command's trust boundary.
    gates = [r for r in results if r["kind"] == "gate"]
    gate = gates[0] if gates else None
    raw_gate_verdict = gate["verdict"] if gate else "not-run"
    raw_gate_verdicts = [item["verdict"] for item in gates]
    # Reuse the controller's conservative scalar normalization so aliases
    # such as ``PASS``/``true`` cannot slip through this boundary either.
    if not gates:
        normalized_gate_verdict = "not-run"
        normalized_gate_verdicts: list[str] = []
    else:
        try:
            from edaloop.loop.controller import _normalize_gate_verdict

            normalized_gate_verdicts = []
            for item in gates:
                normalized = _normalize_gate_verdict(item["verdict"])
                # A syntactically valid PASS emitted with a non-zero process
                # status is not a trustworthy checker result.  Keep the raw
                # report, but classify the diagnostic result as unknown.
                if item.get("rc", 0) != 0 and normalized == "pass":
                    normalized = "unknown"
                normalized_gate_verdicts.append(normalized)
        except Exception:  # pragma: no cover - defensive import fallback
            normalized_gate_verdicts = [
                str(item["verdict"] or "unknown").strip().lower() for item in gates
            ]
        # A duplicate/extra gate must not hide a worse result from the first
        # one.  Keep the aggregate conservative and deterministic.
        rank = {"not-run": 0, "pass": 1, "unverified": 1, "unknown": 2, "blocked": 3, "fail": 4}
        normalized_gate_verdict = max(
            normalized_gate_verdicts,
            key=lambda value: rank.get(value, 2),
        )
    effective_gate_verdict = (
        "unverified" if normalized_gate_verdict == "pass" else normalized_gate_verdict
    )
    verification = {
        "status": "unverified",
        "verified": False,
        "mode": "low-level-experimental",
        "raw_verdict": raw_gate_verdict,
        "raw_verdicts": raw_gate_verdicts,
        "normalized_verdict": normalized_gate_verdict,
        "reason": (
            "stage_apply bypasses LoopController terminal layout snapshot, "
            "gate-stage contract, and delivery artifact checks; use `edaloop run` "
            "for an engineering result"
        ),
    }
    for gate_item, normalized in zip(gates, normalized_gate_verdicts):
        # Preserve the original connector value beside the effective result.
        # Consumers displaying results.json can now distinguish a checker FAIL
        # from a checker PASS that was intentionally not trusted.
        gate_item["raw_verdict"] = gate_item["verdict"]
        gate_item["normalized_verdict"] = normalized
        gate_item["verdict"] = "unverified" if normalized == "pass" else normalized
    audit.event(
        "stage-apply-contract",
        raw_gate_verdict=raw_gate_verdict,
        raw_gate_verdicts=raw_gate_verdicts,
        normalized_gate_verdict=normalized_gate_verdict,
        normalized_gate_verdicts=normalized_gate_verdicts,
        gate_verdict=effective_gate_verdict,
        verified=False,
        reason=verification["reason"],
    )
    audit.save_json("results.json", results)
    failures = [
        r for r in results
        if r["kind"] == "block-apply"
        and _apply_status_is_failure(r.get("status"), r.get("manifest") or {})
    ]
    return {
        "plan_id": plan.id,
        "results": results,
        "gate_verdict": effective_gate_verdict,
        "raw_gate_verdict": raw_gate_verdict,
        "raw_gate_verdicts": raw_gate_verdicts,
        "verified": False,
        "mode": "low-level-experimental",
        "verification": verification,
        "apply_failures": failures,
        "audit_dir": str(audit.dir),
    }
