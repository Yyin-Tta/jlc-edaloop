from __future__ import annotations

import json
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
                        evidence=f"round {round_no}: block-apply 存在失败(autoconnect 连线失败或环境错误,详见 apply-error 审计);本轮 spacing={600 + (round_no - 1) * 150}",
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

    def _apply_zone_frames(self, round_no: int, zone_designators: dict[str, list[str]], actions) -> None:
        """P4-1② 功能分区编排:zones clear → set(真实位号) → zone-plan(审计) → zone-draw → 分区注记。

        注释层操作,单次执行不走通用重试通道(note 重跑会产生重复注释);失败不判负
        (分区框是注释不是电气对象,按弱信号处理),全部入审计;zone-plan 五项校验计数
        留作 P4-4 门禁接线的数据源,本轮只记不拦。
        """
        from edaloop.generate.adapter import AdapterError

        try:
            rc, _, _ = self.adapter.run(["sch", "zones", "clear"])
            self.audit.event("zones-clear", round_no=round_no, rc=rc)
            set_args = ["sch", "zones", "set"]
            for claim, desigs in sorted(zone_designators.items()):
                zone_vocab = CLAIM_ZONE.get(claim, ("center", claim))[0]
                uniq = list(dict.fromkeys(d for d in desigs if d))
                set_args += ["--module", f"{claim}={zone_vocab}:{','.join(uniq)}"]
            rc, out, _ = self.adapter.run(set_args)
            self.audit.event(
                "zones-set",
                round_no=round_no,
                rc=rc,
                claims={c: len(v) for c, v in zone_designators.items()},
                out=(out or "")[:300],
            )
            try:
                plan = self._run_json_retry(["sch", "zone-plan", "--json"])
                validation = plan.get("validation") or {}
                self.audit.event(
                    "zone-plan",
                    round_no=round_no,
                    validation=validation,
                    partitions=len(plan.get("partitions", []) or []),
                )
            except AdapterError as e:
                self.audit.event("zone-plan-error", round_no=round_no, error=str(e)[:500])
            rc, _, _ = self.adapter.run(["sch", "zone-draw", "--mode", "partition"])
            self.audit.event("zone-draw", round_no=round_no, rc=rc)
            # 每带一条分区注记:带说明 + 块名串;锚点取该带最左块 x,y 贴底(y-UP,内容自 300 起)
            band_x: dict[str, int] = {}
            band_names: dict[str, list[str]] = {}
            for act in actions:
                if not act.zone or act.kind not in ("block-apply", "sch-place"):
                    continue
                try:
                    if act.kind == "block-apply":
                        x = int(act.args[act.args.index("--at") + 1].split(",")[0])
                    else:
                        x = int(act.args[act.args.index("--x") + 1])
                except (ValueError, IndexError):
                    continue
                band_x[act.zone] = min(band_x.get(act.zone, x), x)
                band_names.setdefault(act.zone, []).append(
                    act.desc.split(" @")[0].split("(")[0].strip()
                )
            for claim, x in sorted(band_x.items()):
                label = CLAIM_ZONE.get(claim, ("", claim))[1]
                text = f"{label}: " + " / ".join(dict.fromkeys(band_names.get(claim, [])))
                rc, _, _ = self.adapter.run(
                    ["sch", "note", "--text", text, "--x", str(x), "--y", "150", "--zone", claim]
                )
                self.audit.event("zone-note", round_no=round_no, claim=claim, rc=rc, text=text[:200])
        except AdapterError as e:
            self.audit.event("zones-fatal", round_no=round_no, error=str(e)[:1000])

    def _apply(self, actions, round_no: int) -> tuple[bool, dict | None]:
        from edaloop.generate.adapter import AdapterError

        self._warmup()
        ok_all = True
        gate_report = None
        uuids: dict[str, tuple[str, str]] = {}
        failed: set[str] = set()
        place_pinouts: dict[str, dict[str, str]] = {}
        designators: dict[str, str] = {}
        zone_designators: dict[str, list[str]] = {}  # P4-1②:claim → 本轮落图位号
        for act in actions:
            try:
                if act.kind == "sch-gate":
                    if self.zones_enabled and zone_designators and not self.dry_run:
                        self._apply_zone_frames(round_no, zone_designators, actions)
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
                    if ok and act.zone:
                        zone_designators.setdefault(act.zone, []).append(desig)
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
                if status == "applied" and act.zone:
                    for p in manifest.get("placed", []) or []:
                        if p.get("designator"):
                            zone_designators.setdefault(act.zone, []).append(p["designator"])
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
