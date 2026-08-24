from __future__ import annotations

import json
import math
import re
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from edaloop.generate.adapter import EasyedaAdapter
from edaloop.generate.audit import AuditLog
from edaloop.generate.compile import CLAIM_ZONE, compile_actions
from edaloop.generate.models import BlockPlan
from edaloop.generate.plan import ensure_std_candidates, make_plan
from edaloop.intent.ir import DesignIR
from edaloop.knowledge.models import BlockRecord
from edaloop.loop.attribution import attribute
from edaloop.validate.checks import validate
from edaloop.validate.models import Finding

MAX_ROUNDS = 5
SAME_CODE_HALT = 2


def _snap5(raw: float) -> int:
    """snap-5 且远离零取整(就近取整可差 1~2 单位仍越界/仍叠)。"""
    if raw == 0:
        return 0
    n = int(math.ceil(abs(raw) / 5.0) * 5)
    return n if raw > 0 else -n


def _clamp_delta(lo, hi, band_lo: float, band_hi: float) -> int:
    """把 [lo,hi] 钳回 [band_lo,band_hi] 的位移(0=已在带内)。"""
    if lo is None or hi is None:
        return 0
    raw = 0.0
    if hi > band_hi:
        raw = band_hi - hi
    elif lo < band_lo:
        raw = band_lo - lo
    return _snap5(raw) if raw else 0


# clusters 报告缺 sheetUsable 时的回退带 = planner 契约带(plan.py 提示词与
# attribution.py 同源承诺 x∈[100,1100] y∈[300,780]);严格内嵌实测真带
# [12,12]-[1158,813] 且避图签带(y≥198)。2026-08-24 req-01 daily 实证:图框
# 几何丢失时上游 group-arrange 拒排 + 本地无带静默退 → 两轮零修复 HALT;
# 回退此带保住拒排兜底链(宽度足够 group-move 扫描落位,不依赖上游报告)。
_FALLBACK_SHEET_USABLE = (100.0, 300.0, 1100.0, 780.0)  # minX, minY, maxX, maxY


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
        answer_context: str = "",
        retry_queries: list[str] | None = None,
        acceptance_items: list | None = None,
    ) -> None:
        self.ir = ir
        self.catalog = catalog
        self.retrieve = retrieve
        self.llm = llm
        self.adapter = adapter
        self.audit = audit
        self.max_rounds = max_rounds
        self.dry_run = dry_run
        self.answer_context = answer_context
        self.retry_queries = list(retry_queries or [])
        # P4-5①:验收条目(「## 期望指标」标注段,run 不再丢弃;空=无标注段)
        self.acceptance_items = list(acceptance_items or [])
        # P4-1② 功能分区编排(声明+整页分区框+分区注记),默认关——真机验证过再转默认开(风险 R17)
        self.zones_enabled = os.environ.get("EDALOOP_ZONES", "") in ("1", "true", "yes")

    def _cost_hint(self, candidates) -> str:
        """同功能可互换块的价格对比(实时查询,弱信号;仅 IR 有 cost_target 时生成,无诉求不查)。"""
        try:
            from edaloop.generate.bomcost import cost_hint_for_planner

            if not (self.ir.env and self.ir.env.cost_target):
                return ""
            groups: dict[str, list[dict]] = {}
            by_cat: dict[str, list] = {}
            for b in candidates:
                if b.lcsc:
                    by_cat.setdefault(b.category or "misc", []).append(b)
            interchangeable = {
                "interface": ("can", "rs485", "usb"),
                "power": ("ldo", "buck", "boost"),
            }
            for cat, keys in interchangeable.items():
                for key in keys:
                    parts = [
                        {"block_id": b.block_id, "lcsc": b.lcsc}
                        for b in by_cat.get(cat, [])
                        if key in b.block_id.lower() or key in b.name.lower()
                    ]
                    if len(parts) >= 2:
                        groups[f"{cat}:{key}"] = parts
            if not groups:
                return ""
            return cost_hint_for_planner(groups)
        except Exception:
            return ""

    def _augment_freeform(self, plan: BlockPlan, candidates, round_no: int) -> BlockPlan:
        """确定性拓扑模式增强:plan 中该功能仍是 uncovered 且模式原料在检索候选中时,
        用模式库分解结果替换(LLM 兜底失败时的可靠通道;LLM 已分解则不重复)。"""
        from edaloop.generate.freeform import decompose, match_pattern

        text = self.ir.query_text() + " " + (self.ir.source or "")
        pat = match_pattern(text)
        if pat is None:
            return plan
        prefix = pat["id"].split("-")[0]
        needed = {part["block_id"] for part in pat["parts"]}
        plan_ids = {b.block_id for b in plan.blocks}
        if needed <= plan_ids:
            return plan
        # 功能等价判重:LLM 已用 upstream 整块覆盖同功能时不高边注入
        # (如 up-pmos_highside_softstart 覆盖 highside-switch 模式)
        func_tokens = {
            "highside-switch": ("highside", "高边", "负载开关"),
            "reverse-polarity": ("reverse", "防反接", "xl1509", "vehicle_input"),
            "usb-esd": ("usblc", "esd"),
            "lowvolt-alarm": ("tl431", "lowbat", "alarm"),
        }
        func_words = func_tokens.get(pat["id"], ())
        if func_words and any(w in bid for bid in plan_ids for w in func_words):
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
        if not self.dry_run:
            # 版本门前移(P5-0):此前只有 stage_apply 查版本,run 主链裸奔——
            # 真机 daily 在钉扎 0.25.1/实装 1.1.1 漂移下照常落图。真机首次
            # 变更前统一过门(ADR-0002);Fake/测试适配器无此方法则跳过。
            _check = getattr(self.adapter, "check_version", None)
            if callable(_check):
                _check()
        for round_no in range(1, self.max_rounds + 1):
            rec = RoundRecord(round_no=round_no)
            query = self.ir.query_text()
            digest = self.ir.decisions_digest()
            if digest:
                query = query + "\n" + digest
            candidates = list(self.retrieve(query))
            if self.retry_queries and round_no == 1:
                seen = {c.block_id for c in candidates}
                for rq in self.retry_queries:
                    for c in self.retrieve(rq):
                        if c.block_id not in seen:
                            candidates.append(c)
                            seen.add(c.block_id)
                self.audit.event("refine-retry", round_no=1, queries=self.retry_queries, candidates=len(candidates))
            # P4-4②:std R/C 通道常驻(提示词宣传的通道,检索没召回也要可用,否则目录外校验必杀)
            candidates = ensure_std_candidates(candidates, self.catalog)
            plan = make_plan(
                self.ir,
                candidates,
                self.llm,
                feedback=feedback,
                cost_hint=self._cost_hint(candidates),
                answer_context=self.answer_context,
            )
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
                # A4 标定(2026-08 真机):250 为实测可整块入图的格距;旧 600+150×(r-1)
                # 爬坡阶梯废弃——页流下放大 spacing 直接破页容量,重试走 per-block at/params.spacing
                actions = compile_actions(plan, self.catalog, spacing_default="250")
                pages = self._plan_pages(actions)
                self.adapter.clear_all_pages()
                # sch clear 只清各窗口当前活动页;上轮逐页 gate 会把前台留在末页,
                # 故每轮显式清文档全部既有页(含 P1 与超出本轮计划的孤儿页),
                # 否则 r≥2 叠上轮墨迹 → 文档级位号冲突(C8 类)确定性复发。
                existing = self._ensure_pages([p for p in pages if p != "P1"], round_no)
                # 清页保真(2026-08-21 决定性实验结论):sch clear --doc 本身不说谎
                # (连发六页全部真清空,remaining=0 如实),但其结果是三态——幸存时只往
                # result 塞 warning 仍 rc=0;且 r≥2 的清页紧跟上轮 apply,上游实证
                # 「block-apply 后立即 clear 可复现留 ~20 幸存者,数秒后手跑才能清空」。
                # rc 不可信:clear 后回读数器件才算数,幸存 → 重清一次(settle 电阻),
                # 两趟仍不清 → clear-fidelity 失败进审计(不静默;后续 apply 失败自会
                # 经 GATE_FAIL 归因,此处只负责把证据钉死)。
                clear_failed = [
                    p for p in self._page_order(existing | set(pages))
                    if not self._clear_page_verified(p, round_no)
                ]
                self.audit.event("page-clear", round_no=round_no, pages=pages, failures=clear_failed)
                apply_ok, gate_report = self._apply(actions, round_no)
                rec.gate_verdict = gate_report.get("verdict", "unknown") if gate_report else "not-run"
            # P4-4① sizing 轮内化:make_plan 后 validate 段计算(轨输入走 IR,出处随建议入审计),
            # PARAM_OFF_SPEC 弱观察与 feedback 注入都消费它;PASS 后 deliver 复用末轮结果。
            sizing_advices = self._size_round(plan, round_no)
            if round_no == 1 and self.acceptance_items:
                # P4-5①:验收条目进审计(标注段不再丢弃;复评结果随 round-validate 的 weak)
                self.audit.event(
                    "acceptance",
                    items=[
                        {"id": it.id, "source": it.source, "kind": it.kind, "check": it.check,
                         "checker": it.checker, "key": it.key}
                        for it in self.acceptance_items
                    ],
                )
            findings = validate(
                self.ir, plan, gate_report, catalog=self.catalog,
                sizing=sizing_advices or None, acceptance=self.acceptance_items or None,
            )
            self._last_acceptance_unmet = [f for f in findings if f.code == "ACCEPTANCE_UNMET"]
            if not apply_ok:
                findings = [
                    Finding(
                        code="GATE_FAIL",
                        evidence=f"round {round_no}: block-apply 存在失败(autoconnect 连线失败或环境错误,详见 apply-error 审计);本轮 spacing=250(A4 页流;RELAYOUT 反馈请给 at/params.spacing)",
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
                weak_codes=[f.code for f in findings if f.weak],  # P4-4④:与 weak 同序,refine 按码挑问题
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
            # P4-4① sizing 输出经 feedback 注入下轮(值类建议带表内可用值提示,planner 采纳时
            # 用 resistor-std/capacitor-std 落图;只在还有下一轮时有意义,PASS 轮不经过此处)
            siz_fb = self._sizing_feedback(sizing_advices)
            if siz_fb:
                feedback = feedback + "\n" + siz_fb
            rec.feedback = feedback
        self.audit.event("loop-done", status="FAIL", rounds=self.max_rounds)
        return result

    def _size_round(self, plan, round_no: int) -> list:
        """P4-4①:本轮计划的确定性 sizing(轨输入走 IR;失败不阻断,只落审计)。"""
        try:
            from edaloop.generate.sizing import size_for_plan

            advices = size_for_plan(plan.blocks, ir=self.ir, catalog=self.catalog)
            self._last_sizing = advices
            self.audit.event(
                "sizing",
                round_no=round_no,
                advices=[
                    {
                        "kind": a.kind, "target": a.target, "rec": a.result_rec,
                        "rec_value": a.rec_value, "rec_kind": a.rec_kind, "nets": list(a.nets),
                        "inputs": [list(i) for i in a.inputs],
                    }
                    for a in advices
                ],
            )
            return advices
        except Exception as e:  # sizing 是弱增强:任何失败不拖垮主链路
            self._last_sizing = []
            self.audit.event("sizing-error", round_no=round_no, error=str(e)[:200])
            return []

    def _sizing_feedback(self, advices: list) -> str:
        """值类建议的 planner 反馈行(带标准件表命中状态;表内值才可直接落图)。"""
        try:
            from edaloop.generate.stdparts import lookup

            kind_map = {"resistance": "resistor", "capacitance": "capacitor"}
            lines = []
            for a in advices:
                if a.result_rec == "n/a":
                    continue
                if a.rec_value and a.rec_kind in kind_map:
                    hit = lookup(kind_map[a.rec_kind], a.rec_value)
                    tail = f"params.value={a.rec_value}" if hit else f"推荐 {a.rec_value}(就近换表内值)"
                    lines.append(f"- {a.kind}@{a.target}: {tail}")
                else:
                    lines.append(f"- {a.kind}@{a.target}: {a.result_rec}")
            if not lines:
                return ""
            return (
                "sizing 建议值(确定性公式,输入出处见 delivery.sizing.txt;采纳时用 "
                "resistor-std/capacitor-std 块 + params.value 表内标准值,值不要发明):\n" + "\n".join(lines[:10])
            )
        except Exception:
            return ""

    def deliver(self, result) -> dict:
        """PASS 后交付打包:SVG + 网表 + BOM 成本 + 摘要落 run 目录(§1 交付链路)。"""
        if result.status != "PASS" or self.dry_run:
            return {}
        import hashlib

        arts = {}
        try:
            # export-image 缺省只导前台页(末轮逐页 gate 把前台留在末页)——多页必须
            # 逐页 --doc 导出,否则交付物静默缺页(P1 电源页丢失类);单页保持原名。
            pages = sorted(
                {b.page or "P1" for b in (result.final_plan.blocks if result.final_plan else [])}
            )
            exported: list[str] = []
            for p in pages:
                name = "delivery.svg" if len(pages) == 1 else f"delivery-{p}.svg"
                svg_path = str((self.audit.dir / name).resolve())
                rc, _, _ = self.adapter.run(
                    ["sch", "export-image", "--out", svg_path, "--format", "svg", "--doc", p]
                )
                if rc == 0 and Path(svg_path).exists():
                    exported.append(svg_path)
            if exported:
                arts["svg"] = exported[0]
                arts["svg_pages"] = exported
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
        try:
            from edaloop.generate.bomcost import summarize_bom

            placed: list[dict] = []
            for b in result.final_plan.blocks if result.final_plan else []:
                rec = self.catalog.get(b.block_id)
                part_refs = [
                    {"instance": f"{b.instance}:{p.ref}", "block_id": b.block_id, "lcsc": p.lcsc or ""}
                    for p in (rec.parts if rec else [])
                ]
                if part_refs:
                    placed.extend(part_refs)
                else:
                    placed.append(
                        {"instance": b.instance, "block_id": b.block_id, "lcsc": (rec.lcsc if rec else "") or ""}
                    )
            if placed:
                bom = summarize_bom(placed)
                try:
                    from edaloop.generate.selection import annotate_smt

                    lcscs = sorted({p["lcsc"] for p in placed if p.get("lcsc")})
                    smt = annotate_smt(lcscs)
                    for det in bom.get("details", []):
                        det["smt_type"] = smt.get(det.get("ref"), "unknown")
                    bom["smt_note"] = "库类型近似判定(JLC SMT API 无公开契约,R13 兜底):basic=基础库(免上料费倾向),extended=扩展库"
                except Exception as e:
                    self.audit.event("smt-annotate-error", error=str(e)[:150])
                (self.audit.dir / "delivery.bom.json").write_text(
                    json.dumps(bom, ensure_ascii=False, indent=1), encoding="utf-8"
                )
                arts["bom"] = str(self.audit.dir / "delivery.bom.json")
                arts["bom_total"] = bom.get("total")
        except Exception as e:
            self.audit.event("bom-cost-error", error=str(e)[:200])
        try:
            from edaloop.generate.selection import proposals_report, propose_swaps

            groups: dict[str, list[dict]] = {}
            for b in result.final_plan.blocks if result.final_plan else []:
                rec = self.catalog.get(b.block_id)
                if rec and rec.lcsc:
                    key = (rec.category or "misc").lower()
                    groups.setdefault(key, []).append({"block_id": b.block_id, "lcsc": rec.lcsc})
            groups = {k: v for k, v in groups.items() if len(v) >= 2}
            report = proposals_report(propose_swaps(groups)) if groups else "(无等价类组,跳过 swap 分析)"
            (self.audit.dir / "delivery.swap.txt").write_text(report, encoding="utf-8")
            arts["swap"] = str(self.audit.dir / "delivery.swap.txt")
        except Exception as e:
            self.audit.event("swap-error", error=str(e)[:200])
        try:
            # P4-4①:deliver 复用末轮轮内 sizing(缺则重算一次;输出口径含输入来源表)
            advices = getattr(self, "_last_sizing", None)
            if advices is None:
                from edaloop.generate.sizing import size_for_plan

                blocks_dicts = [
                    {"block_id": b.block_id, "instance": b.instance, "ports_binding": b.ports_binding}
                    for b in (result.final_plan.blocks if result.final_plan else [])
                ]
                advices = size_for_plan(blocks_dicts, ir=self.ir, catalog=self.catalog)
            if advices:
                (self.audit.dir / "delivery.sizing.txt").write_text(
                    "\n\n".join(a.render() for a in advices), encoding="utf-8"
                )
                arts["sizing"] = str(self.audit.dir / "delivery.sizing.txt")
                arts["sizing_count"] = len(advices)
        except Exception as e:
            self.audit.event("sizing-error", error=str(e)[:200])
        try:
            # P4-5①:验收清单交付(条目 + 末轮复评结果;manual 条目照列,人审)
            if self.acceptance_items:
                from edaloop.intent.acceptance import is_executable

                unmet = {f.where.ref for f in getattr(self, "_last_acceptance_unmet", [])}
                lines = []
                for it in self.acceptance_items:
                    mark = "✗ " if it.id in unmet else ("· " if is_executable(it.checker) else "? ")
                    lines.append(f"{mark}[{it.id}]({it.source}/{it.kind}) {it.check} → {it.checker}\n    期望: {it.expect}")
                for f in getattr(self, "_last_acceptance_unmet", []):
                    lines.append(f"  ✗ {f.evidence[:160]}")
                (self.audit.dir / "delivery.acceptance.txt").write_text(
                    "验收清单(✗=机械复评未满足 · =可执行已过 ? =manual 人审)\n" + "\n".join(lines),
                    encoding="utf-8",
                )
                arts["acceptance"] = str(self.audit.dir / "delivery.acceptance.txt")
        except Exception as e:
            self.audit.event("acceptance-error", error=str(e)[:200])
        try:
            from edaloop.loop.critic import render_report, review_plan

            if result.final_plan and result.final_plan.blocks:
                catalog_desc = {k: v.desc for k, v in self.catalog.items()}
                # P4-4④ 输入增强:网表摘要 + IR rails + sizing 建议值(critique 输入不再只有 plan 骨架)
                net_summary = ""
                try:
                    net_json = arts.get("netlist") and Path(arts["netlist"]).read_text(encoding="utf-8") or ""
                    if net_json:
                        net = json.loads(net_json)
                        nets = net.get("nets") or net.get("netlist") or []
                        names = [n.get("name", n.get("net", "")) if isinstance(n, dict) else str(n) for n in nets]
                        net_summary = f"{len(names)} nets: " + ", ".join(sorted(filter(None, names))[:60])
                except Exception:
                    net_summary = ""
                rails_summary = "; ".join(
                    f"{r.name}={r.v_text()}" + (f" imax={r.imax:g}A" if r.imax is not None else "")
                    for r in self.ir.power.rails
                )
                sizing_summary = "\n".join(
                    f"{a.kind}@{a.target}: {a.result_rec}" for a in (getattr(self, "_last_sizing", None) or []) if a.result_rec != "n/a"
                )
                findings = review_plan(
                    result.final_plan,
                    self.llm,
                    catalog_desc=catalog_desc,
                    netlist_summary=net_summary,
                    rails_summary=rails_summary,
                    sizing_summary=sizing_summary,
                )
                summary = f"{len(result.final_plan.blocks)} blocks, status={result.status}"
                (self.audit.dir / "delivery.review.txt").write_text(
                    render_report(findings, summary), encoding="utf-8"
                )
                arts["review"] = str(self.audit.dir / "delivery.review.txt")
                arts["review_findings"] = len(findings)
                self.audit.event("critic", findings=[f.model_dump() for f in findings])
        except Exception as e:
            self.audit.event("critic-error", error=str(e)[:200])
        self.audit.event("delivery", artifacts=arts)
        return arts

    def _verify_pins(self, round_no: int, designator: str, pinout: dict[str, str] | None, page: str = "P1") -> bool:
        """place 后回读符号 pin 集合,与库 pinout diff(三方校验的落地端);--page 定页读。"""
        if not pinout:
            return True
        try:
            read = self._run_json_retry(["sch", "read", "--page", page or "P1"])
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
    def _jitter_at(args: list[str], delta: int = 40) -> list[str]:
        """轮内重试时对 --at 坐标做确定性偏移(避开原冲突几何;A4 尺度下 40 ≈ 半格距)。"""
        try:
            i = args.index("--at")
            x, y = args[i + 1].split(",")
            args = list(args)
            args[i + 1] = f"{int(x) + delta},{int(y) + delta}"
        except (ValueError, IndexError):
            pass
        return args

    @staticmethod
    def _page_order(names) -> list[str]:
        """页名规范序:P1 恒首,其余 P<n> 按号升序,非规范名殿后。"""

        def order(p: str) -> tuple[int, int]:
            m = re.fullmatch(r"P(\d+)", p.strip())
            if p == "P1":
                return (0, 0)
            return (1, int(m.group(1))) if m else (2, 0)

        return sorted(set(names), key=order)

    @classmethod
    def _plan_pages(cls, actions) -> list[str]:
        """动作涉及的落图页(P1 恒首,其余按页号升序;无落图动作回退 P1)。

        块按高度重排后动作流首现序不保证 P1 打头(实测 P4 曾打头,调用方
        pages[1:] 误把 P4 当 P1 跳过 → 漏建页 → --doc 落图全炸);
        建页/清页/逐页 gate/复核统一消费本序。
        """
        raw = {a.page or "P1" for a in actions if a.kind in ("block-apply", "sch-place")}
        return cls._page_order(raw) or ["P1"]

    def _ensure_pages(self, want: list[str], round_no: int) -> set[str]:
        """P4-b2 多页提前量:compile 判定分页后,落图前按名建页(幂等,已存在跳过)。

        P5-0 增页修剪:eval 复用同一工程,page-clear 只清内容不删页 → 页数单调涨
        (实测 38 页后 EasyEDA netlist 导出超时"returned no file",block-apply 内置
        净验证全缺脚假阴性 → GATE_FAIL,2026-08-22 实证);先删计划外的 P\\d+ 孤儿页
        再建页。page-new 无名(上游 v0.25.1 无 --name),需 page-rename 两段;变更类
        命令单次执行不走重试通道(重试会双建页);失败只审计不判负——后续 --doc 落图
        命令会显式失败并走既有 apply-fatal 路径。
        返回修剪+建页后文档剩余页名(调用方据此逐页全清;非 harness 命名的页不动,
        仍走清内容路径)。
        """
        from edaloop.generate.adapter import AdapterError

        self._warmup()
        try:
            info = self._run_json_retry(["sch", "pages"])
        except AdapterError as e:
            self.audit.event("pages-read-error", round_no=round_no, error=str(e)[:500])
            return set()
        res = info.get("result", {}) or {}
        entries = list(res.get("pages") or [])
        if not entries:
            for s in res.get("schematics", []) or []:
                entries.extend(s.get("page", []) or [])
        have = {str(p.get("name", "")).strip() for p in entries if str(p.get("name", "")).strip()}
        sch_uuid = next((p.get("parentSchematicUuid") for p in entries if p.get("parentSchematicUuid")), "")
        # P5-0 页修剪:只删 harness 命名形态(^P\\d+$)且不在本轮计划的页;单发不重试
        # (变更类纪律,同 page-new);删失败不判负,残留页由逐页清内容路径兜底。
        keep = set(want) | {"P1"}
        by_name = {str(p.get("name", "")).strip(): p.get("uuid", "") for p in entries}
        doomed = sorted(n for n in have if re.match(r"^P\d+$", n) and n not in keep)
        deleted: list[str] = []
        prune_failed: list[str] = []
        for name in doomed:
            uuid = by_name.get(name, "")
            try:
                if not uuid:
                    raise AdapterError(f"sch pages 未返回该页 uuid: {name}")
                rc, _, _ = self.adapter.run(["sch", "page-delete", "--page", uuid])
                if rc == 0:
                    deleted.append(name)
                else:
                    prune_failed.append(name)
            except Exception as e:  # noqa: BLE001 - 删页失败不判负,残留页走清内容路径
                prune_failed.append(name)
                self.audit.event("page-prune-error", round_no=round_no, name=name, error=str(e)[:300])
        if doomed:
            self.audit.event("page-prune", round_no=round_no, deleted=deleted, failed=prune_failed)
            have -= set(deleted)
        for name in want:
            if name in have:
                continue
            try:
                if not sch_uuid:
                    raise AdapterError("sch pages 未返回 parentSchematicUuid,无法建页")
                r = self.adapter.run_json(["sch", "page-new", "--schematic", sch_uuid])
                inner = r.get("result", r) if isinstance(r, dict) else {}
                page_uuid = (inner or {}).get("pageUuid") or (inner or {}).get("uuid") or ""
                if not page_uuid:
                    raise AdapterError(f"page-new 未返回 uuid: {str(r)[:200]}")
                rc, _, _ = self.adapter.run(["sch", "page-rename", "--page", page_uuid, "--name", name])
                self.audit.event("page-create", round_no=round_no, name=name, uuid=page_uuid, rc=rc)
                if rc == 0:
                    have.add(name)  # 仅成功改名才计入(失败页留着由 --doc 落图显式暴露)
            except Exception as e:  # noqa: BLE001 - 建页失败不判负,交由后续 --doc 命令显式暴露
                self.audit.event("page-create-error", round_no=round_no, name=name, error=str(e)[:500])
        return have

    def _gate_all_pages(self, gate_args: list[str], actions, round_no: int) -> dict:
        """逐页 gate:上游 gate 只校验活动页,多页必须 --doc 逐页跑;verdict 取最坏,stages 并集带页标。"""
        from edaloop.generate.adapter import AdapterError

        worst = {"pass": 0, "unknown": 1, "blocked": 1, "fail": 2}
        merged: dict = {"verdict": "pass", "stages": []}
        for p in self._plan_pages(actions):
            verdict_page = "unknown"
            stages_page: list = []
            try:
                rep = self._run_json_retry(list(gate_args) + ["--doc", p])
                verdict_page = rep.get("verdict", "unknown")
                stages_page = rep.get("stages", []) or []
            except AdapterError as e:
                self.audit.event("gate-error", round_no=round_no, page=p, error=str(e)[:500])
                verdict_page = "blocked"
            if worst.get(verdict_page, 2) > worst.get(merged["verdict"], 2):
                merged["verdict"] = verdict_page
            merged["stages"].extend([dict(s, page=p) for s in stages_page])
            self.audit.event(
                "gate",
                round_no=round_no,
                page=p,
                verdict=verdict_page,
                stages=[
                    f"{s.get('stage') or s.get('name')}:{s.get('verdict') or s.get('status')}"
                    for s in stages_page
                ],
            )
        return merged

    @staticmethod
    def _doc_args(act) -> list[str]:
        """页钉扎:一切带页的落图动作(含 P1)追加全局 --doc(上游无 --page,--doc 是唯一
        页选择器,CLI 自动切页并核对 document.current,refuse 而落错页)。

        P1 不豁免:--doc 切换是粘性的,前台可能停在上一动作切去的页——不带 --doc 的
        变更命令会落错页且与该页首块锚点(100,300)精确相撞(2026-08-21 req-02 真机
        三轮全灭的根因)。sch-gate 除外:_gate_all_pages 自行逐页钉扎。
        """
        if act.page and act.kind != "sch-gate":
            return act.args + ["--doc", act.page]
        return act.args

    def _apply_zone_frames(self, round_no: int, zone_pages: dict[str, dict[str, list[str]]], actions) -> None:
        """P4-1②/P4-b2 功能分区编排(逐页):zones clear → set(真实位号) → zone-plan(审计)
        → zone-draw → 分区注记。

        注释层操作,单次执行不走通用重试通道(note 重跑会产生重复注释);失败不判负
        (分区框是注释不是电气对象,按弱信号处理),全部入审计;zone-plan 五项校验计数
        留作 P4-4 门禁接线的数据源,本轮只记不拦。注记锚 (100+i*350, 230):避图签
        keepout(y≤198 且 x≥468)且各认领横向错开防 label 碰撞。
        """
        from edaloop.generate.adapter import AdapterError

        for page, claims in sorted(zone_pages.items()):
            doc = ["--doc", page]
            page_actions = [a for a in actions if (a.page or "P1") == page]
            try:
                rc, _, _ = self.adapter.run(["sch", "zones", "clear", *doc])
                self.audit.event("zones-clear", round_no=round_no, page=page, rc=rc)
                set_args = ["sch", "zones", "set"]
                for claim, desigs in sorted(claims.items()):
                    zone_vocab = CLAIM_ZONE.get(claim, ("center", claim))[0]
                    uniq = list(dict.fromkeys(d for d in desigs if d))
                    set_args += ["--module", f"{claim}={zone_vocab}:{','.join(uniq)}"]
                rc, out, _ = self.adapter.run(set_args + doc)
                self.audit.event(
                    "zones-set",
                    round_no=round_no,
                    page=page,
                    rc=rc,
                    claims={c: len(v) for c, v in claims.items()},
                    out=(out or "")[:300],
                )
                validation: dict = {}
                plan_ok = False
                try:
                    plan = self._run_json_retry(["sch", "zone-plan", "--json", *doc])
                    plan_ok = True
                    validation = plan.get("validation") or {}
                    self.audit.event(
                        "zone-plan",
                        round_no=round_no,
                        page=page,
                        validation=validation,
                        partitions=len(plan.get("partitions", []) or []),
                    )
                except AdapterError as e:
                    self.audit.event("zone-plan-error", round_no=round_no, page=page, error=str(e)[:500])
                # partitionOverlap 非 0 = 两区体积真互压(上游定论)→ zone-draw 必拒。
                # zone-arrange --apply 是上游专用解(断言①删除=重建 → 落位重连 →
                # 断言② 曾连 pin 仍连 → lint+bridge-check,任一红逐步回滚);重排后
                # 重 plan 再画,仍脏则 draw 照旧拒、归反馈域(弱信号不判负)。
                fixable = ("sheetOverflow", "partitionOverlap", "titleBlockHits", "sheetMarginHits")
                if plan_ok and any(validation.get(k) for k in fixable):
                    rc_za, out_za, err_za = self.adapter.run(["sch", "zone-arrange", "--apply", *doc])
                    self.audit.event(
                        "zone-arrange",
                        round_no=round_no,
                        page=page,
                        rc=rc_za,
                        out=(out_za or "")[-300:],
                        error=(err_za or "")[-300:],
                    )
                    try:
                        plan = self._run_json_retry(["sch", "zone-plan", "--json", *doc])
                        validation = plan.get("validation") or {}
                        self.audit.event(
                            "zone-plan",
                            round_no=round_no,
                            page=page,
                            validation=validation,
                            partitions=len(plan.get("partitions", []) or []),
                        )
                    except AdapterError as e:
                        self.audit.event("zone-plan-error", round_no=round_no, page=page, error=str(e)[:500])
                rc, _, _ = self.adapter.run(["sch", "zone-draw", "--mode", "partition", *doc])
                self.audit.event("zone-draw", round_no=round_no, page=page, rc=rc)
                for i, claim in enumerate(sorted(claims)):
                    label = CLAIM_ZONE.get(claim, ("", claim))[1]
                    names = [
                        a.desc.split(" @")[0].split("(")[0].strip()
                        for a in page_actions
                        if a.zone == claim and a.kind in ("block-apply", "sch-place")
                    ]
                    text = f"{label}: " + " / ".join(dict.fromkeys(names))
                    rc, _, _ = self.adapter.run(
                        ["sch", "note", "--text", text, "--x", str(100 + i * 350), "--y", "230", "--zone", claim, *doc]
                    )
                    self.audit.event("zone-note", round_no=round_no, page=page, claim=claim, rc=rc, text=text[:200])
            except AdapterError as e:
                self.audit.event("zones-fatal", round_no=round_no, page=page, error=str(e)[:1000])

    def _clusters_report(self, page: str) -> dict:
        """clusters --json 全量报告(带几何/簇 box/flags),解析失败返回空。

        rc!=0 是常态(ERROR 即非零),解析只认 stdout;不带 --strict:tight 是
        WARN,属 gap 参数域,不触发拆排。"""
        _, out, _ = self.adapter.run(["sch", "clusters", "--json", "--doc", page])
        try:
            return json.loads(out) if (out or "").strip() else {}
        except ValueError:
            return {}

    def _cluster_errors(self, page: str) -> list[dict]:
        rep = self._clusters_report(page)
        return [f for f in (rep.get("findings") or []) if f.get("level") == "ERROR"]

    def _arrange_closeout(
        self,
        round_no: int,
        placed_by_page: dict[str, dict[str, list[str]]],
        zone_by_page: dict[str, dict[str, list[str]]] | None = None,
    ) -> None:
        """P4-b3 布局收口(逐页,仅 clusters ERROR 页):拆问题块组 → 逐件单件组
        → group-arrange 刚体平移(网表逐 pin 不变)→ 复查,仍 ERROR 换大 gap 重排一次。

        为什么拆到单件:block-apply 封组让整块成为刚体,块内碰撞(usbc J1
        netport 翼展压 D3,校准 A 实测)任何整块平移都修不掉;单件化后
        arrange 按耦合逐件落位(usbc 结构死局实证 2 ERROR→0)。干净块保持
        整组刚体、干净页整页不动(重排会把已达标几何重洗,收益为负)。
        副作用已知并接受:snap-5 平移可产生 marker 微叠(大 gap 重排兜底);
        移动内核清扫不重建 NC 标(floating=warn 弱门禁;no-connect 有引入
        真短路的实锤,绝不自动补)。全程非致命:收口失败只审计,gate 是最终权威。"""
        from edaloop.generate.adapter import AdapterError

        for page, insts in sorted(placed_by_page.items()):
            try:
                errs = self._cluster_errors(page)
                if not errs:
                    continue
                self.audit.event(
                    "arrange-probe",
                    round_no=round_no,
                    page=page,
                    errors=[{"type": f.get("type"), "a": f.get("a"), "b": f.get("b")} for f in errs],
                )
                # ERROR 位号点名谁拆谁;对不上号(位号漂移/上轮残留)→ 组全拆
                err_desigs = {d for f in errs for d in (f.get("a"), f.get("b")) if d}
                self._shatter_groups(round_no, page, insts, err_desigs)
                # gap 梯子自适应(run5/run6 实证):执行过仍脏(rc=0)是微叠类 → 放大;
                # 拒排 rc=1 是装不下类(run6 实测 P2 总需仅超带 4~24 单位)→ 逐档缩小
                # 60→40。仍救不回(P1/P6 类:arrange 的组占地含挂线,拆成单件也不缩
                # 翼展——run6 现场实验 7 单件仍拒)→ 钳回兜底刚移点名件
                gap, tried = 80, []
                for _ in range(3):
                    rc, out, err = self.adapter.run(
                        ["sch", "group-arrange", "--annotate=false", "--gap", str(gap), "--doc", page]
                    )
                    tried.append(gap)
                    self.audit.event(
                        "arrange-apply",
                        round_no=round_no,
                        page=page,
                        gap=gap,
                        rc=rc,
                        out=(out or "")[-300:],
                        error=(err or "")[-300:],
                    )
                    errs = self._cluster_errors(page)
                    if not errs:
                        break
                    if rc == 0:
                        nxt = 140 if 140 not in tried else None
                    else:
                        nxt = next((g for g in (60, 40) if g not in tried), None)
                    if nxt is None:
                        break
                    gap = nxt
                if errs:
                    errs = self._clamp_into_band(round_no, page, (zone_by_page or {}).get(page))
                self.audit.event("arrange-result", round_no=round_no, page=page, remaining=len(errs))
            except AdapterError as e:
                self.audit.event("arrange-fatal", round_no=round_no, page=page, error=str(e)[:800])

    def _clamp_into_band(
        self, round_no: int, page: str, zone_map: dict[str, list[str]] | None = None
    ) -> list[dict]:
        """拒排兜底:按 clusters 可用带把 ERROR 件刚移到空位。

        为什么这条路成立:gate/clusters 的 sheetUsable 含图签带(实测
        [12,12]-[1158,813],801 高),比 arrange 的排布带([12,198] 起,615 高)
        宽——收口的验收是 clusters 零 ERROR,不是过 arrange;arrange 拒排时
        它什么都没动,点名件还在带外,刚移(group-move 挂线跟随)即可。

        一次一动:每步后重探再决策(实测 P6 三件连钳,J4 落在 J3 上、R10 压
        R9——钳回带内 ≠ 钳到空位)。落点沿钳回轴向带内扫 60 步进避邻居,
        有 zone 认领时优先落本 zone 包络旁(run7 残留 partitionOverlap=3 实证
        裸钳会把件甩进邻居分区);overlap 双方都在带内 → b 沿 y 下推/上推 40
        分离。(位号,dx,dy) 发过即入 spent 不再重发——拒过的重发=4 次空转
        (run8 P1/P2 实证),成功过的重发=几何绕圈回原状的振荡(req-05 P3 J1
        实证 -180/+180/-180 循环)。无解就停,归反馈域。"""
        zone_map = zone_map or {}
        claim_of = {d: claim for claim, ds in zone_map.items() for d in ds}
        spent: set[tuple[str, int, int]] = set()  # 拒过的+已发过的(位号,dx,dy),都不再发
        # 预算随首轮 ERROR 数伸缩:固定 4 次「一次一动」在 7 件越界页必剩 3 件
        # (req-05 P1 实锤:探针 7 件、钳 4 件、remaining=3,r2 整页重排同形 →
        # 同码连胜 HALT);×2 余量吸收移动牵出的新 ERROR。退出条件不变——清零
        # 即返/无可动即返,预算纯防呆上限,不影响收敛判定。
        errs = self._cluster_errors(page)
        if not errs:
            return []
        for _ in range(max(4, 2 * len(errs))):
            rep = self._clusters_report(page)
            errs = [f for f in (rep.get("findings") or []) if f.get("level") == "ERROR"]
            if not errs:
                return []
            u = rep.get("sheetUsable") or {}
            try:
                ux1, uy1, ux2, uy2 = (float(u[k]) for k in ("minX", "minY", "maxX", "maxY"))
            except (KeyError, TypeError, ValueError):
                # 旧:带几何缺失静默返回(无证据不动作)。clusters 无带 → 回退
                # planner 契约带并留痕,ERROR 件仍可刚移——拒排兜底链不断。
                self.audit.event(
                    "clamp-band-fallback",
                    round_no=round_no,
                    page=page,
                    missing=str(u)[:120],
                )
                ux1, uy1, ux2, uy2 = _FALLBACK_SHEET_USABLE
            boxes = {
                c.get("designator"): c.get("box")
                for c in (rep.get("clusters") or [])
                if c.get("designator")
            }
            # 位号 → 所在组(shatter 后点名件应为单件组;组=刚移单位,挂线自动跟随)
            _, out, _ = self.adapter.run(["sch", "group", "list", "--json", "--doc", page])
            try:
                grp_rep = json.loads(out) if (out or "").strip() else {}
                groups = [g for gs in (grp_rep.get("groupsByPage") or {}).values() for g in gs]
            except ValueError:
                groups = []
            gid_of: dict[str, str] = {}
            for g in groups:
                gid = g.get("id") or g.get("name") or ""
                for m in g.get("members") or []:
                    d = m.get("designator")
                    if d and gid and d not in gid_of:
                        gid_of[d] = gid
            acted = False

            def _zone_bbox(finding: dict) -> tuple[float, float, float, float] | None:
                """点名件同 zone 其他成员的联合包络(自身除外;无箱或独居 → None)。"""
                d = finding.get("a") or ""
                members = zone_map.get(claim_of.get(d, ""), [])
                bb = [boxes[m] for m in members if m != d and boxes.get(m)]
                if not bb:
                    return None
                return (
                    min(x["minX"] for x in bb),
                    min(x["minY"] for x in bb),
                    max(x["maxX"] for x in bb),
                    max(x["maxY"] for x in bb),
                )

            for f in errs:
                cands = self._clamp_moves_for(f, boxes, gid_of, ux1, uy1, ux2, uy2, _zone_bbox(f))
                move = next((c for c in cands if (c[0], c[2], c[3]) not in spent), None)
                if not move:
                    if not cands:
                        # 无候选诊断(req-05 P2 U4 实锤:探针点名却永不动作,离线无从
                        # 知道是缺箱/缺组/带内无空位)——把判定依据钉进审计再谈修复
                        d = f.get("a") or ""
                        b = boxes.get(d) or {}
                        self.audit.event(
                            "clamp-no-candidate",
                            round_no=round_no,
                            page=page,
                            type=f.get("type"),
                            designator=d,
                            has_box=bool(b),
                            in_group=d in gid_of,
                            box=str(b)[:120],
                        )
                    continue
                d, gid, dx, dy = move
                rc, _, err = self.adapter.run(
                    ["sch", "group-move", "--group", gid, "--dx", str(dx), "--dy", str(dy), "--doc", page]
                )
                # 已发过的(成功与否)不再发:成功位移重推导=几何绕了一圈回到原状
                # (req-05 P3 J1 实锤:-180/+180/-180 循环),spent 断环路
                spent.add((d, dx, dy))
                acted = True
                self.audit.event(
                    "arrange-clamp",
                    round_no=round_no,
                    page=page,
                    cause=f.get("type"),
                    designator=d,
                    group=gid,
                    dx=dx,
                    dy=dy,
                    rc=rc,
                    error=(err or "")[-200:],
                )
                break  # 一次一动,下一步用新鲜几何
            if not acted:
                return errs
        rep = self._clusters_report(page)
        return [f for f in (rep.get("findings") or []) if f.get("level") == "ERROR"]

    def _clamp_moves_for(
        self,
        finding: dict,
        boxes: dict[str, dict],
        gid_of: dict[str, str],
        ux1: float,
        uy1: float,
        ux2: float,
        uy2: float,
        zone_bbox: tuple[float, float, float, float] | None = None,
    ) -> list[tuple[str, str, int, int]]:
        """一条 ERROR → 候选刚移序列 [(位号,组id,dx,dy)] 按偏好序;无解 []。

        内核才是落点权威(run8 P1/P2 实证:clusters 带含图签条,按带查可行
        的下推目标仍可撞图签 keepout 被钳成 Δ0;连线树共享邻件 pin 则整组
        拒移、与方向无关),所以这里交全序候选,调用方逐个试、拒过的不再发。
        out-of-sheet:钳回带内,落点沿移动轴向内扫 60 步进避邻居,zone 认领
        时按离包络中心最近排序(无认领保持最小位移优先);
        overlap:先 b 后 a,各先下推 40 再上推(下推可行性按新 minY 整箱
        查——旧版查 maxY 会放过半出带的目标,keepout 拒移即源于此)。"""
        margin = 15.0

        def occupied(d: str, b: dict) -> bool:
            return any(
                d != o
                and b["minX"] - margin < ob.get("maxX", 1e9)
                and b["maxX"] + margin > ob.get("minX", -1e9)
                and b["minY"] - margin < ob.get("maxY", 1e9)
                and b["maxY"] + margin > ob.get("minY", -1e9)
                for o, ob in boxes.items()
            )

        if finding.get("type") == "out-of-sheet":
            d = finding.get("a")
            b = boxes.get(d or "")
            if not b or d not in gid_of:
                return []
            dx = _clamp_delta(b.get("minX"), b.get("maxX"), ux1, ux2)
            dy = _clamp_delta(b.get("minY"), b.get("maxY"), uy1, uy2)
            if dx == dy == 0:
                return []
            # 2D 网格扫描(req-05 P2 实锤:轴锁死扫 6 步全被占,±120 横偏仍不够
            # 大件落位):主轴=越界轴(双出界取量大者),钳回后同向 60 步进×8
            # 深入带内;横轴兜底 0/±60…±360(半幅带)。落点逐个过带界检查,
            # 有空位只进空位;候选序=位移最小优先,zone 认领时再按包络中心重排。
            primary_x = abs(dx) >= abs(dy)

            def _ladder(delta: int) -> list[int]:
                if delta:
                    s = -60 if delta < 0 else 60
                    return [delta + i * s for i in range(8)]
                return [0]

            def _cross() -> list[int]:
                return [x for m in range(7) for x in ((m * 60), (-m * 60))][1:]

            in_band: list[tuple[tuple[int, int], dict]] = []
            for m in _ladder(dx if primary_x else dy):
                for c in _cross():
                    cand = (m, c + dy) if primary_x else (c + dx, m)
                    tb = {
                        "minX": b["minX"] + cand[0],
                        "maxX": b["maxX"] + cand[0],
                        "minY": b["minY"] + cand[1],
                        "maxY": b["maxY"] + cand[1],
                    }
                    if (
                        tb["minX"] >= ux1
                        and tb["maxX"] <= ux2
                        and tb["minY"] >= uy1
                        and tb["maxY"] <= uy2
                    ):
                        in_band.append((cand, tb))
            free = [(cand, tb) for cand, tb in in_band if not occupied(d, tb)]
            if not free:
                # 带内无空位兜底(req-05 P2 U4 实锤:239×412 簇箱在带内被整页
                # 簇箱铺满,±360×8 级扫描仍零空位):改选「带内+压叠数最少」落点,
                # 把 out-of-sheet 降级成 overlap——交给下一轮探针的 overlap 钳
                # 沿 y 分移;b 侧 40 步分离 + spent 断环,不会无限拉锯。
                def _n_overlaps(bb: dict) -> int:
                    return sum(
                        1
                        for o, ob in boxes.items()
                        if d != o
                        and bb["minX"] - margin < ob.get("maxX", 1e9)
                        and bb["maxX"] + margin > ob.get("minX", -1e9)
                        and bb["minY"] - margin < ob.get("maxY", 1e9)
                        and bb["maxY"] + margin > ob.get("minY", -1e9)
                    )

                ranked = sorted(
                    in_band,
                    key=lambda ct: (_n_overlaps(ct[1]), abs(ct[0][0]) + abs(ct[0][1])),
                )[:8]
                if not ranked:
                    return []
                return [(d, gid_of[d], c[0], c[1]) for c, _ in ranked]
            if zone_bbox:
                zx = (zone_bbox[0] + zone_bbox[2]) / 2
                zy = (zone_bbox[1] + zone_bbox[3]) / 2
                free.sort(
                    key=lambda ct: ((ct[1]["minX"] + ct[1]["maxX"]) / 2 - zx) ** 2
                    + ((ct[1]["minY"] + ct[1]["maxY"]) / 2 - zy) ** 2
                )
            return [(d, gid_of[d], c[0], c[1]) for c, _ in free]
        if finding.get("type") == "overlap":
            a, b_ = finding.get("a"), finding.get("b")
            ba, bb = boxes.get(a or ""), boxes.get(b_ or "")
            if not ba or not bb:
                return []
            moves: list[tuple[str, str, int, int]] = []
            for p, q in ((b_, a), (a, b_)):  # 先动 b;b 不可动再动 a
                if p not in gid_of:
                    continue
                bp, bq = boxes[p], boxes[q]
                down = _snap5(bq["minY"] - 40 - bp["maxY"])  # p 下移到 q 下方 40
                if down and bp["minY"] + down >= uy1:
                    moves.append((p, gid_of[p], 0, down))
                up = _snap5(bq["maxY"] + 40 - bp["minY"])  # p 上移到 q 上方 40
                if up and bp["maxY"] + up <= uy2:
                    moves.append((p, gid_of[p], 0, up))
            return list(dict.fromkeys(moves))  # 同(位号,dx,dy)去重保序
        return []

    def _shatter_groups(
        self, round_no: int, page: str, insts: dict[str, list[str]], err_desigs: set[str]
    ) -> None:
        """按 ERROR 点名拆组:问题件的封组解体 → 点名件单件化(arrange 获得逐件
        自由度),无辜件重新封组保持刚体——块作者标定几何少受扰动,参与 arrange
        的体积也小(run5 实证整块全拆=6-9 组在 y∈[198,813] 可用带装不下,
        点名拆能把排布体积压回可容纳)。err_desigs 对不上任何落图位号(位号
        漂移/上轮残留)→ 该页所有组全拆(拆多不拆错:漏拆=整块仍刚体,块内
        碰撞永远修不掉)。

        另:place 通道件不自动归组,不属于任何组的落图件补建单件组——
        否则 arrange 对它们零手段(out-of-sheet 永存,run5 P3/P4 实证)。"""
        _, out, _ = self.adapter.run(["sch", "group", "list", "--json", "--doc", page])
        try:
            rep = json.loads(out) if (out or "").strip() else {}
            groups = [g for gs in (rep.get("groupsByPage") or {}).values() for g in gs]
        except ValueError:
            groups = []
        all_placed = {d for ds in insts.values() for d in ds}
        targeted = bool(err_desigs & all_placed)
        covered: set[str] = set()
        for g in groups:
            gid = g.get("id") or g.get("name") or ""
            members = [m.get("designator", "") for m in (g.get("members") or []) if m.get("designator")]
            if not gid or not members:
                continue
            covered |= set(members)
            culprits = set(members) & err_desigs if targeted else set(members)
            if not culprits:
                continue  # 干净组:整组保持刚体
            rest = sorted(set(members) - culprits)
            self.adapter.run(["sch", "group", "ungroup", "--group", gid, "--doc", page])
            for d in sorted(culprits):
                self.adapter.run(["sch", "group", "create", "--members", d, "--doc", page])
            if len(rest) >= 2:
                self.adapter.run(["sch", "group", "create", "--members", ",".join(rest), "--doc", page])
            elif rest:
                self.adapter.run(["sch", "group", "create", "--members", rest[0], "--doc", page])
            self.audit.event(
                "arrange-shatter",
                round_no=round_no,
                page=page,
                group=gid,
                culprits=sorted(culprits),
                rest=rest,
            )
        for d in sorted(all_placed - covered):
            self.adapter.run(["sch", "group", "create", "--members", d, "--doc", page])
            self.audit.event("arrange-stray-group", round_no=round_no, page=page, designator=d)

    def _apply_titleblocks(self, round_no: int, actions) -> None:
        """逐页明细表只读保位(注释类非致命)。

        上游 0.26.0 明令 titleblock --data 写入禁令:该写路径触发图签重建、
        损毁 sheet 符号引用 → 重启后图框丢失 → group-arrange 拒动 → overlap
        修不掉 → HALT(2026-08-24 req-01 daily 定案,时序 09:59 写/11:48 强杀
        重启/13:21 全灭)。标题文字的收益(一条注释)远低于该链路代价,不再由
        harness 写入;--show 只是可见性开关(非禁令写路径),失败只审计不判负。"""
        from edaloop.generate.adapter import AdapterError

        for page in self._plan_pages(actions):
            try:
                rc, out, err = self.adapter.run(["sch", "titleblock", "--show", "--doc", page])
                self.audit.event(
                    "titleblock",
                    round_no=round_no,
                    page=page,
                    show_rc=rc,
                    out=(out or "")[-200:],
                    error=(err or "")[-120:],
                )
            except AdapterError as e:
                self.audit.event("titleblock-error", round_no=round_no, page=page, error=str(e)[:500])

    def _apply(self, actions, round_no: int) -> tuple[bool, dict | None]:
        from edaloop.generate.adapter import AdapterError

        self._warmup()
        ok_all = True
        gate_report = None
        uuids: dict[str, tuple[str, str]] = {}
        failed: set[str] = set()
        place_pinouts: dict[str, dict[str, str]] = {}
        designators: dict[str, str] = {}
        zone_designators: dict[str, dict[str, list[str]]] = {}  # P4-1②/P4-b2:页 → claim → 本轮落图位号
        placed_by_page: dict[str, dict[str, list[str]]] = {}  # P4-b3:页 → 实例 → 落图位号(拆组重排用)
        for act in actions:
            try:
                args = self._doc_args(act)  # P4-b2:非 P1 页追加 --doc 钉扎
                if act.kind == "sch-gate":
                    # P4-b3 收口次序:拆组重排 → 分区框 → 明细表,全部先于 gate
                    # (zone-draw 按落图后几何画框、明细表作用前台页,重排必须最先)
                    if not self.dry_run:
                        self._arrange_closeout(round_no, placed_by_page, zone_designators)
                        if self.zones_enabled and zone_designators:
                            self._apply_zone_frames(round_no, zone_designators, actions)
                        self._apply_titleblocks(round_no, actions)
                    gate_report = self._gate_all_pages(act.args, actions, round_no)
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
                    place_args = []
                    holes = iter((lib, uuid))
                    for x in args:
                        place_args.append(next(holes) if x == "" else x)
                    resp = self._run_json_retry(place_args)
                    comp = (resp.get("result", {}) or {}).get("component", {}) or {}
                    desig = comp.get("designator", "")
                    ok = bool(desig)
                    if ok:
                        placed_by_page.setdefault(act.page or "P1", {}).setdefault(act.block_instance, []).append(desig)
                    if ok and act.zone:
                        zone_designators.setdefault(act.page or "P1", {}).setdefault(act.zone, []).append(desig)
                    if ok and act.pinout:
                        ok = self._verify_pins(round_no, desig, act.pinout, act.page or "P1")
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
                        page=act.page or "P1",
                        ok=ok,
                    )
                    continue
                manifest: dict = {}
                if act.kind == "sch-autoconnect":
                    rc, _, _ = self.adapter.run(args)
                    status = "applied" if rc == 0 else "failed"
                else:
                    # block-apply 非幂等:manifest 只从本次执行 stdout 解析,
                    # 绝不重发(重放=同页孪生再放一份,详见 _run_manifest_once)
                    manifest = self._run_manifest_once(args)
                    status = manifest.get("ok") or manifest.get("status") or "unknown"
                self.audit.event(
                    act.kind,
                    round_no=round_no,
                    instance=act.block_instance,
                    status=status,
                    failure=manifest.get("failure", "") or "",
                    window=getattr(self.adapter, "window_id", ""),
                    page=act.page or "P1",
                    args=args if act.kind in ("block-apply", "sch-place") else [],
                )
                if status != "applied":
                    if str(status).startswith("failed-partial"):
                        survivors = manifest.get("rollback", {}).get("survivedPrimitiveIds", [])
                        if survivors:
                            self.adapter.delete_primitives(survivors)
                            self.audit.event("cleanup", round_no=round_no, deleted=survivors)
                        try:
                            retry_args = self._jitter_at(args)
                            manifest = self._run_manifest_once(retry_args)
                            status = manifest.get("ok") or manifest.get("status") or "unknown"
                            self.audit.event(
                                act.kind,
                                round_no=round_no,
                                instance=act.block_instance,
                                status=status,
                                retry=True,
                                page=act.page or "P1",
                                args=retry_args if act.kind == "block-apply" else [],
                            )
                        except AdapterError as e:
                            self.audit.event(
                                "apply-fatal",
                                round_no=round_no,
                                instance=act.block_instance,
                                error=str(e)[:1500],
                            )
                if status == "applied":
                    des = [p["designator"] for p in manifest.get("placed", []) or [] if p.get("designator")]
                    if des:
                        placed_by_page.setdefault(act.page or "P1", {})[act.block_instance] = des
                    if act.zone:
                        zone_designators.setdefault(act.page or "P1", {}).setdefault(act.zone, []).extend(des)
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
        机械复核(逐页 --page 读,多页合并):计划网络全部存在于网表且页面非空 → 判 applied。"""
        comps: list[dict] = []
        page_nets: set[str] = set()
        try:
            for p in self._plan_pages(actions):
                read = self._run_json_retry(["sch", "read", "--page", p])
                res = read.get("result", {}) or {}
                comps.extend(c for c in res.get("components", []) if c.get("componentType") != "sheet")
                page_nets |= {str(n.get("net") or n.get("name") or "") for n in res.get("nets", [])}
        except AdapterError:
            return False
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

        ir_families = {_rail_family(r.name or r.v_text()) for r in self.ir.power.rails}
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

    def _page_component_count(self, page: str) -> int:
        """sch read --page 回读非 sheet 器件数;读失败返回 -1(未知 ≠ 已清空)。"""
        try:
            read = self._run_json_retry(["sch", "read", "--page", page])
        except Exception as e:  # AdapterError 等:读不到按未知处理,绝不当作已清空
            self.audit.event("clear-verify-error", page=page, error=str(e)[:500])
            return -1
        res = read.get("result", {}) or {}
        return sum(1 for c in res.get("components", []) if c.get("componentType") != "sheet")

    def _clear_page_verified(self, page: str, round_no: int) -> bool:
        """clear --doc 后机械复核:result.remaining(自报)+ 回读数器件(实证)双证据;
        任一不净 → 重清一次;两趟仍不清 → False(审计留痕)。"""
        for attempt in (1, 2):
            rc, out, _ = self.adapter.run(["sch", "clear", "--doc", page])
            remaining: int | None = None
            warnings: list[str] = []
            try:
                result = (json.loads(out) or {}).get("result", {}) or {}
                remaining = result.get("remaining")
                warnings = [str(w)[:200] for w in (result.get("warnings") or [])][:3]
            except ValueError:
                pass  # 非 JSON 输出:remaining 维持未知,判定交给回读
            survivors = self._page_component_count(page)
            ok = rc == 0 and survivors == 0 and remaining in (None, 0)
            self.audit.event(
                "page-clear-doc",
                round_no=round_no,
                page=page,
                rc=rc,
                remaining=remaining,
                survivors=survivors,
                warnings=warnings,
                attempt=attempt,
                ok=ok,
            )
            if ok:
                return True
        return False

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

    def _run_manifest_once(self, args) -> dict:
        """变更型命令(block-apply)只许执行一次,manifest 取该次 stdout。

        禁止走 _run_json_retry 重发:同参 block-apply 重放会在已落图的页上
        再放一份同几何部件(平台只给新位号 D4/J2/R3…),apply 内置 verify
        逐件报 overlap+pin coincidence → 整单判死回滚,第一遍成品被误记
        failed-rolled-back;若重放的推让恰好躲开同位,verify 漏抓、双份留
        页,拖到逐页 gate 才炸(run3b r1/r2 六连挂根因;2026-08-21 连接器
        审计对账 + 活体复现 D1↔D4…R2↔R4 六对孪生后钉死)。"""
        from edaloop.generate.adapter import AdapterError

        rc, out, err = self.adapter.run(args)
        try:
            return json.loads(out) if out.strip() else {}
        except ValueError as e:
            raise AdapterError(
                f"block-apply stdout 非 JSON(rc={rc},{e}):stderr 尾部={err[-500:]}"
            ) from e

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
