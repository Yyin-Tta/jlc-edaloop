from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from edaloop.generate.adapter import EasyedaAdapter
from edaloop.generate.audit import AuditLog
from edaloop.generate.compile import compile_actions
from edaloop.generate.models import BlockPlan
from edaloop.generate.plan import make_plan
from edaloop.intent.ir import DesignIR
from edaloop.knowledge.models import BlockRecord
from edaloop.loop.attribution import attribute
from edaloop.validate.checks import validate
from edaloop.validate.models import Finding

MAX_ROUNDS = 5
SAME_CODE_HALT = 2


@dataclass
class RoundRecord:
    round_no: int
    plan_id: str = ""
    gate_verdict: str = "not-run"
    findings: list[Finding] = field(default_factory=list)
    feedback: str = ""
    halted: str = ""


@dataclass
class LoopResult:
    status: str
    rounds: list[RoundRecord] = field(default_factory=list)
    final_plan: BlockPlan | None = None
    audit_dir: str = ""

    @property
    def converged_round(self) -> int | None:
        if self.status == "PASS":
            return len(self.rounds)
        return None


class LoopController:
    def __init__(
        self,
        ir: DesignIR,
        catalog: dict[str, BlockRecord],
        retrieve,
        llm,
        adapter: EasyedaAdapter,
        audit: AuditLog,
        *,
        max_rounds: int = MAX_ROUNDS,
        dry_run: bool = False,
    ) -> None:
        self.ir = ir
        self.catalog = catalog
        self.retrieve = retrieve
        self.llm = llm
        self.adapter = adapter
        self.audit = audit
        self.max_rounds = max_rounds
        self.dry_run = dry_run

    def run(self) -> LoopResult:
        result = LoopResult(status="FAIL", audit_dir=str(self.audit.dir))
        feedback = ""
        code_streak: dict[str, int] = {}
        for round_no in range(1, self.max_rounds + 1):
            rec = RoundRecord(round_no=round_no)
            query = self.ir.query_text()
            candidates = self.retrieve(query)
            plan = make_plan(self.ir, candidates, self.llm, feedback=feedback)
            rec.plan_id = plan.id
            self.audit.event(
                "round-plan",
                round_no=round_no,
                plan_id=plan.id,
                blocks=[b.instance for b in plan.blocks],
                uncovered=plan.uncovered,
                feedback=feedback,
            )
            gate_report = None
            apply_ok = True
            if not self.dry_run:
                if round_no > 1:
                    self.adapter.run(["sch", "clear"])
                    self.audit.event("page-clear", round_no=round_no)
                spacing = str(400 + (round_no - 1) * 100)
                actions = compile_actions(plan, self.catalog, spacing_default=spacing)
                apply_ok, gate_report = self._apply(actions, round_no)
                rec.gate_verdict = gate_report.get("verdict", "unknown") if gate_report else "not-run"
            findings = validate(self.ir, plan, gate_report)
            if not apply_ok:
                findings = [
                    Finding(
                        code="GATE_FAIL",
                        evidence=f"round {round_no}: block-apply 存在失败(autoconnect 连线失败或环境错误,详见 apply-error 审计);本轮 spacing={400 + (round_no - 1) * 100}",
                        severity="error",
                        suggested_fix_class="RELAYOUT",
                    )
                ] + findings
            rec.findings = findings
            blocking = [f for f in findings if not f.weak]
            self.audit.event(
                "round-validate",
                round_no=round_no,
                gate=rec.gate_verdict,
                blocking=[f.model_dump() for f in blocking],
                weak=[f.evidence for f in findings if f.weak],
            )
            result.rounds.append(rec)
            result.final_plan = plan
            if not blocking:
                result.status = "PASS"
                self.audit.event("loop-done", status="PASS", rounds=round_no)
                return result
            streak_key = "|".join(sorted({f.code for f in blocking}))
            code_streak[streak_key] = code_streak.get(streak_key, 0) + 1
            if code_streak[streak_key] >= SAME_CODE_HALT:
                result.status = "HALT"
                rec.halted = f"同错 {SAME_CODE_HALT} 轮:{streak_key},升级人工"
                self.audit.event("loop-halt", round_no=round_no, reason=rec.halted)
                return result
            feedback = attribute(blocking)
            rec.feedback = feedback
        self.audit.event("loop-done", status="FAIL", rounds=self.max_rounds)
        return result

    def _apply(self, actions, round_no: int) -> tuple[bool, dict | None]:
        from edaloop.generate.adapter import AdapterError

        self._warmup()
        ok_all = True
        gate_report = None
        uuids: dict[str, tuple[str, str]] = {}
        failed: set[str] = set()
        for act in actions:
            try:
                if act.kind == "sch-gate":
                    gate_report = self._run_json_retry(act.args)
                    verdict = gate_report.get("verdict", "unknown")
                    stage_summary = [
                        f"{s.get('stage') or s.get('name')}:{s.get('verdict') or s.get('status')}"
                        for s in gate_report.get("stages", [])
                    ]
                    self.audit.event(
                        "gate",
                        round_no=round_no,
                        verdict=verdict,
                        stages=stage_summary,
                    )
                    continue
                if act.kind == "lib-search":
                    resp = self._run_json_retry(act.args)
                    lib, uuid = self._first_uuid(resp)
                    if not lib:
                        ok_all = False
                        failed.add(act.block_instance)
                        self.audit.event(
                            "apply-fatal",
                            round_no=round_no,
                            instance=act.block_instance,
                            error=f"lib search 无结果: {act.lcsc}",
                        )
                        continue
                    uuids[act.block_instance] = (lib, uuid)
                    self.audit.event(
                        "lib-search", round_no=round_no, instance=act.block_instance, lib=lib, uuid=uuid
                    )
                    continue
                if act.block_instance in failed:
                    continue
                if act.kind == "sch-place":
                    lib, uuid = uuids.get(act.block_instance, ("", ""))
                    if not lib:
                        ok_all = False
                        failed.add(act.block_instance)
                        continue
                    args = []
                    holes = iter((lib, uuid))
                    for x in act.args:
                        args.append(next(holes) if x == "" else x)
                    resp = self._run_json_retry(args)
                    comp = (resp.get("result", {}) or {}).get("component", {}) or {}
                    ok = bool(comp.get("designator"))
                    if not ok:
                        ok_all = False
                        failed.add(act.block_instance)
                    self.audit.event(
                        "sch-place",
                        round_no=round_no,
                        instance=act.block_instance,
                        designator=comp.get("designator", "?"),
                        ok=ok,
                    )
                    continue
                rc, out, err = self.adapter.run(act.args)
                manifest: dict = {}
                if act.kind == "sch-autoconnect":
                    status = "applied" if rc == 0 else "failed"
                else:
                    try:
                        manifest = self._run_json_retry(act.args)
                    except AdapterError as e:
                        ok_all = False
                        self.audit.event(
                            "apply-fatal",
                            round_no=round_no,
                            instance=act.block_instance,
                            error=str(e)[:2000],
                        )
                        continue
                    status = manifest.get("ok") or manifest.get("status") or "unknown"
                self.audit.event(
                    act.kind, round_no=round_no, instance=act.block_instance, status=status
                )
                if status != "applied":
                    ok_all = False
                    if str(status).startswith("failed-partial"):
                        survivors = manifest.get("rollback", {}).get("survivedPrimitiveIds", [])
                        if survivors:
                            self.adapter.delete_primitives(survivors)
                            self.audit.event("cleanup", round_no=round_no, deleted=survivors)
            except AdapterError as e:
                ok_all = False
                self.audit.event(
                    "apply-fatal",
                    round_no=round_no,
                    instance=act.block_instance,
                    error=str(e)[:2000],
                )
        return ok_all, gate_report

    @staticmethod
    def _first_uuid(resp: dict) -> tuple[str, str]:
        res = resp.get("result", {}) or {}
        comps = res.get("components") or res.get("results") or []
        for r in comps:
            lib = r.get("libraryUuid") or r.get("lib") or ""
            uuid = r.get("uuid") or r.get("deviceUuid") or ""
            if lib and uuid:
                return lib, uuid
        return "", ""

    def _warmup(self, attempts: int = 3, delay: float = 5.0) -> None:
        """廉价读命令预热连接器 WS,把空闲重连竞态消化在 block-apply 之前。"""
        for i in range(attempts):
            try:
                rc, out, _ = self.adapter.run(["sch", "pages"])
                if rc == 0:
                    return
            except Exception:
                pass
            time.sleep(delay)

    def _run_json_retry(self, args, attempts: int = 2, delay: float = 8.0) -> dict:
        from edaloop.generate.adapter import AdapterError

        last: Exception | None = None
        for i in range(attempts):
            try:
                return self.adapter.run_json(args)
            except AdapterError as e:
                last = e
                self.audit.event("apply-error", attempt=i + 1, error=str(e)[:2000])
                if i + 1 < attempts:
                    time.sleep(delay)
        raise last
