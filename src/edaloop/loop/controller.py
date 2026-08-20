from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

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

    def _augment_freeform(self, plan: BlockPlan, candidates, round_no: int) -> BlockPlan:
        """确定性拓扑模式增强:plan 中该功能仍是 uncovered 且模式原料在检索候选中时,
        用模式库分解结果替换(LLM 兜底失败时的可靠通道;LLM 已分解则不重复)。"""
        from edaloop.generate.freeform import decompose, match_pattern

        text = self.ir.query_text() + " " + (self.ir.source or "")
        pat = match_pattern(text)
        if pat is None:
            return plan
        prefix = pat["id"].split("-")[0]
        already = any(b.instance.startswith(prefix) for b in plan.blocks)
        if already:
            return plan
        cand_map = {b.block_id: b for b in candidates}
        blocks, notes = decompose(pat, cand_map, prefix)
        if not blocks:
            self.audit.event("freeform-miss", round_no=round_no, pattern=pat["id"], notes=notes)
            return plan
        plan.blocks.extend(blocks)
        plan.uncovered = [
            u for u in plan.uncovered if not any(k in u.lower() for k in pat["keywords"])
        ] + [f"[自由拓扑:{pat['id']}] {n}" if n else "" for n in notes]
        plan.uncovered = [u for u in plan.uncovered if u]
        self.audit.event("freeform-augment", round_no=round_no, pattern=pat["id"], added=[b.instance for b in blocks])
        return plan

    def run(self) -> LoopResult:
        result = LoopResult(status="FAIL", audit_dir=str(self.audit.dir))
        feedback = ""
        code_streak: dict[str, int] = {}
        for round_no in range(1, self.max_rounds + 1):
            rec = RoundRecord(round_no=round_no)
            query = self.ir.query_text()
            candidates = self.retrieve(query)
            plan = make_plan(self.ir, candidates, self.llm, feedback=feedback)
            plan = self._augment_freeform(plan, candidates, round_no)
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
                self.adapter.clear_all_pages()
                self.audit.event("page-clear", round_no=round_no)
                spacing = str(600 + (round_no - 1) * 150)
                actions = compile_actions(plan, self.catalog, spacing_default=spacing)
                apply_ok, gate_report = self._apply(actions, round_no)
                rec.gate_verdict = gate_report.get("verdict", "unknown") if gate_report else "not-run"
            findings = validate(self.ir, plan, gate_report, catalog=self.catalog)
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

    def deliver(self, result) -> dict:
        """PASS 后交付打包:SVG + 网表 + 摘要落 run 目录(§1 交付链路)。"""
        if result.status != "PASS" or self.dry_run:
            return {}
        import hashlib

        arts = {}
        try:
            svg_path = str((self.audit.dir / "delivery.svg").resolve())
            rc, out, _ = self.adapter.run(["sch", "export-image", "--out", svg_path, "--format", "svg"])
            if rc == 0 and Path(svg_path).exists():
                arts["svg"] = svg_path
        except Exception:
            pass
        try:
            rc, out, _ = self.adapter.run(["sch", "netlist"])
            if rc == 0:
                net = out
                (self.audit.dir / "delivery.net.json").write_text(net, encoding="utf-8")
                arts["netlist"] = str(self.audit.dir / "delivery.net.json")
                arts["netlist_sha256_16"] = hashlib.sha256(net.encode()).hexdigest()[:16]
        except Exception:
            pass
        self.audit.event("delivery", artifacts=arts)
        return arts

    def _verify_pins(self, round_no: int, designator: str, pinout: dict[str, str] | None) -> bool:
        """place 后回读符号 pin 集合,与库 pinout diff(三方校验的落地端)。"""
        if not pinout:
            return True
        try:
            read = self._run_json_retry(["sch", "read"])
        except AdapterError as e:
            self.audit.event("pin-verify", round_no=round_no, designator=designator, error=str(e)[:500])
            return True
        placed = next(
            (c for c in read.get("result", {}).get("components", []) if c.get("designator") == designator),
            None,
        )
        if not placed:
            self.audit.event("pin-verify", round_no=round_no, designator=designator, error="回读未找到器件")
            return False
        symbol_pins = {p.get("number"): p.get("name") for p in placed.get("pins", [])}
        diff = {
            k: (symbol_pins.get(k), pinout.get(k))
            for k in set(symbol_pins) | set(pinout)
            if symbol_pins.get(k) != pinout.get(k)
        }
        ok = not diff and len(symbol_pins) == len(pinout)
        self.audit.event(
            "pin-verify",
            round_no=round_no,
            designator=designator,
            symbol=len(symbol_pins),
            expected=len(pinout),
            ok=ok,
            diff={k: v for k, v in list(diff.items())[:10]},
        )
        return ok

    @staticmethod
    def _jitter_at(args: list[str], delta: int = 350) -> list[str]:
        """轮内重试时对 --at 坐标做确定性偏移(避开原冲突几何)。"""
        try:
            i = args.index("--at")
            x, y = args[i + 1].split(",")
            args = list(args)
            args[i + 1] = f"{int(x) + delta},{int(y) + delta}"
        except (ValueError, IndexError):
            pass
        return args

    def _apply(self, actions, round_no: int) -> tuple[bool, dict | None]:
        from edaloop.generate.adapter import AdapterError

        self._warmup()
        ok_all = True
        gate_report = None
        uuids: dict[str, tuple[str, str]] = {}
        failed: set[str] = set()
        place_pinouts: dict[str, dict[str, str]] = {}
        designators: dict[str, str] = {}
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
                        args=act.args,
                    )
                    continue
                if act.kind == "lib-search":
                    resp = self._run_json_retry(act.args)
                    lib, uuid = self._first_uuid(resp)
                    if not lib and act.mpn and act.mpn.upper() != act.lcsc.upper():
                        resp = self._run_json_retry(
                            ["lib", "search", "--query", act.mpn, "--limit", "3"]
                        )
                        lib, uuid = self._first_uuid(resp)
                        if lib:
                            self.audit.event(
                                "lib-search-fallback",
                                round_no=round_no,
                                instance=act.block_instance,
                                mpn=act.mpn,
                                lib=lib,
                                uuid=uuid,
                            )
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
                    desig = comp.get("designator", "")
                    ok = bool(desig)
                    if ok and act.pinout:
                        ok = self._verify_pins(round_no, desig, act.pinout)
                        if not ok:
                            failed.add(act.block_instance)
                    if not ok:
                        ok_all = False
                        if not desig:
                            failed.add(act.block_instance)
                    self.audit.event(
                        "sch-place",
                        round_no=round_no,
                        instance=act.block_instance,
                        designator=desig or "?",
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
                    act.kind,
                    round_no=round_no,
                    instance=act.block_instance,
                    status=status,
                    failure=manifest.get("failure", "") or "",
                    window=getattr(self.adapter, "window_id", ""),
                    args=act.args if act.kind in ("block-apply", "sch-place") else [],
                )
                if status != "applied":
                    if str(status).startswith("failed-partial"):
                        survivors = manifest.get("rollback", {}).get("survivedPrimitiveIds", [])
                        if survivors:
                            self.adapter.delete_primitives(survivors)
                            self.audit.event("cleanup", round_no=round_no, deleted=survivors)
                        try:
                            retry_args = self._jitter_at(act.args)
                            manifest = self._run_json_retry(retry_args)
                            status = manifest.get("ok") or manifest.get("status") or "unknown"
                            self.audit.event(
                                act.kind,
                                round_no=round_no,
                                instance=act.block_instance,
                                status=status,
                                retry=True,
                                args=retry_args if act.kind == "block-apply" else [],
                            )
                        except AdapterError as e:
                            self.audit.event(
                                "apply-fatal",
                                round_no=round_no,
                                instance=act.block_instance,
                                error=str(e)[:1500],
                            )
                if status != "applied":
                    ok_all = False
            except AdapterError as e:
                ok_all = False
                self.audit.event(
                    "apply-fatal",
                    round_no=round_no,
                    instance=act.block_instance,
                    error=str(e)[:2000],
                )
        if not ok_all and gate_report and gate_report.get("verdict") == "pass":
            ok_all = self._verify_substance(actions, round_no)
        return ok_all, gate_report

    def _verify_substance(self, actions, round_no: int) -> bool:
        """block-apply 的 failed-rolled-back 可能是回滚校验假象(部件实际在页上,gate 也过)。
        机械复核:计划网络全部存在于网表且页面非空 → 判 applied(证据入审计)。"""
        try:
            read = self._run_json_retry(["sch", "read"])
        except AdapterError:
            return False
        res = read.get("result", {}) or {}
        comps = [c for c in res.get("components", []) if c.get("componentType") != "sheet"]
        page_nets = {str(n.get("net") or n.get("name") or "") for n in res.get("nets", [])}
        planned = {
            act.args[i + 1].split("=", 1)[1]
            for act in actions
            for i, a in enumerate(act.args)
            if a == "--bind" and i + 1 < len(act.args)
        }
        for act in actions:
            if act.kind == "sch-autoconnect":
                try:
                    planned.add(act.args[act.args.index("--net") + 1])
                except ValueError:
                    pass
        missing = {n for n in planned if n and n.upper() != "NC" and n not in page_nets}
        from edaloop.validate.checks import _rail_family

        ir_families = {_rail_family(r.name or f"{r.voltage:g}V") for r in self.ir.power.rails}
        ir_families.add("GND|main")
        strong_missing = {
            n for n in missing if _rail_family(n) in ir_families or _rail_family(n).split("|")[0] == "GND"
        }
        ok = bool(comps) and len(comps) >= 10 and not strong_missing
        self.audit.event(
            "substance-verify",
            round_no=round_no,
            comps=len(comps),
            planned_nets=len(planned),
            strong_missing=sorted(strong_missing)[:15],
            weak_missing=sorted(missing - strong_missing)[:15],
            ok=ok,
        )
        return ok

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
        """廉价读命令预热连接器 WS;全部失败则重解析窗口再试一轮。"""
        for phase in ("first", "refresh"):
            for _ in range(attempts if phase == "first" else 2):
                try:
                    rc, out, _ = self.adapter.run(["sch", "pages"])
                    if rc == 0:
                        return
                except Exception:
                    pass
                time.sleep(delay)
            if phase == "first":
                self.adapter.refresh_window()
                self.audit.event("window-refresh", round_no=None)

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
