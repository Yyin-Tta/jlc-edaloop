from __future__ import annotations

import json
import re
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from edaloop.generate.adapter import EasyedaAdapter
from edaloop.generate.audit import AuditLog
from edaloop.generate.compile import CLAIM_ZONE, compile_actions
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
        answer_context: str = "",
        retry_queries: list[str] | None = None,
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
            findings = validate(self.ir, plan, gate_report, catalog=self.catalog)
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
            from edaloop.generate.sizing import size_for_plan

            blocks_dicts = [
                {"block_id": b.block_id, "instance": b.instance, "ports_binding": b.ports_binding}
                for b in (result.final_plan.blocks if result.final_plan else [])
            ]
            advices = size_for_plan(blocks_dicts)
            if advices:
                (self.audit.dir / "delivery.sizing.txt").write_text(
                    "\n\n".join(a.render() for a in advices), encoding="utf-8"
                )
                arts["sizing"] = str(self.audit.dir / "delivery.sizing.txt")
                arts["sizing_count"] = len(advices)
        except Exception as e:
            self.audit.event("sizing-error", error=str(e)[:200])
        try:
            from edaloop.loop.critic import render_report, review_plan

            if result.final_plan and result.final_plan.blocks:
                catalog_desc = {k: v.desc for k, v in self.catalog.items()}
                findings = review_plan(result.final_plan, self.llm, catalog_desc=catalog_desc)
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

        page-new 无名(上游 v0.25.1 无 --name),需 page-rename 两段;变更类命令
        单次执行不走重试通道(重试会双建页);失败只审计不判负——后续 --doc 落图
        命令会显式失败并走既有 apply-fatal 路径。
        返回建页后文档全部既有页名(调用方据此逐页全清,含孤儿页)。
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
        for act in actions:
            try:
                args = self._doc_args(act)  # P4-b2:非 P1 页追加 --doc 钉扎
                if act.kind == "sch-gate":
                    if self.zones_enabled and zone_designators and not self.dry_run:
                        self._apply_zone_frames(round_no, zone_designators, actions)
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
                if status == "applied" and act.zone:
                    for p in manifest.get("placed", []) or []:
                        if p.get("designator"):
                            zone_designators.setdefault(act.page or "P1", {}).setdefault(act.zone, []).append(p["designator"])
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
