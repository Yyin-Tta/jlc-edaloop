from __future__ import annotations

import json
from pathlib import Path

from edaloop.generate.audit import AuditLog
from edaloop.generate.models import BlockPlan
from edaloop.intent.ir import DesignIR
from edaloop.knowledge.models import BlockRecord, RetrievedBlock, UpstreamRef
from edaloop.llm.fake import FakeChat
from edaloop.loop.attribution import attribute
from edaloop.loop.controller import LoopController
from edaloop.validate.checks import (
    check_gauge,
    check_rails,
    check_topology_sanity,
    check_uncovered,
)
from edaloop.validate.models import Finding, Where


def _ir_with_rails(*volts: tuple[str, float]) -> DesignIR:
    return DesignIR.model_validate(
        {
            "source": "t.md",
            "power": {"rails": [{"name": n, "voltage": v} for n, v in volts]},
        }
    )


def _plan_with_nets(*nets: str) -> BlockPlan:
    return BlockPlan.model_validate(
        {
            "blocks": [
                {
                    "block_id": "ldo-ams1117-3v3",
                    "upstream_id": "block.ams1117_ldo_3v3",
                    "instance": "ldo1",
                    "ports_binding": {n: n for n in nets},
                }
            ]
        }
    )


def test_check_rails_pass_with_alias() -> None:
    ir = _ir_with_rails(("3V3", 3.3), ("5V", 5.0))
    plan = _plan_with_nets("+3V3", "5V")
    assert check_rails(ir, plan) == []


def test_check_rails_iso_family() -> None:
    ir = _ir_with_rails(("5V_ISO", 5.0))
    plan = _plan_with_nets("VISO")
    assert check_rails(ir, plan) == []
    plan2 = _plan_with_nets("+VO")
    assert check_rails(ir, plan2) == []
    plan3 = _plan_with_nets("5V")
    assert check_rails(ir, plan3) != []


def test_check_rails_missing() -> None:
    ir = _ir_with_rails(("3V3", 3.3), ("5V", 5.0))
    plan = _plan_with_nets("3V3")
    findings = check_rails(ir, plan)
    assert len(findings) == 1
    assert findings[0].code == "MISSING_RAIL"
    assert findings[0].where.net == "5V"


def test_check_topology_sanity_power_gnd_bound() -> None:
    catalog = {
        "raw-part": BlockRecord(
            block_id="raw-part",
            name="X",
            desc="x",
            lcsc="C123",
            pinout={"1": "VCC", "2": "GND", "3": "OUT"},
        )
    }
    ok = BlockPlan.model_validate(
        {
            "blocks": [
                {
                    "block_id": "raw-part",
                    "upstream_id": "",
                    "instance": "x1",
                    "pins_binding": {"1": "5V", "2": "GND", "3": "SIG"},
                }
            ]
        }
    )
    assert check_topology_sanity(ok, catalog) == []
    bad = BlockPlan.model_validate(
        {
            "blocks": [
                {
                    "block_id": "raw-part",
                    "upstream_id": "",
                    "instance": "x1",
                    "pins_binding": {"3": "SIG"},
                }
            ]
        }
    )
    findings = check_topology_sanity(bad, catalog)
    assert len(findings) == 2
    assert all(f.code == "PIN_MISMATCH" for f in findings)


def test_check_topology_sanity_skips_upstream_blocks() -> None:
    plan = BlockPlan.model_validate(
        {
            "blocks": [
                {
                    "block_id": "ldo-ams1117-3v3",
                    "upstream_id": "block.ams1117_ldo_3v3",
                    "instance": "ldo1",
                    "ports_binding": {},
                }
            ]
        }
    )
    assert check_topology_sanity(plan, {}) == []


def test_check_uncovered_weak() -> None:
    plan = BlockPlan.model_validate({"uncovered": ["TL431 低压告警"]})
    findings = check_uncovered(plan)
    assert findings[0].weak is True
    assert findings[0].code == "IR_UNCOVERED"


def test_check_gate_pass() -> None:
    assert check_gauge({"verdict": "pass", "stages": []}) == []


def test_check_gate_fail() -> None:
    report = {
        "verdict": "fail",
        "stages": [
            {
                "stage": "layout-lint",
                "verdict": "fail",
                "findings": [{"code": "overlap", "designators": ["U1", "C1"]}],
            }
        ],
    }
    findings = check_gauge(report)
    assert findings and findings[0].code == "GATE_FAIL"
    assert findings[0].suggested_fix_class == "RELAYOUT"


def test_check_gate_blocked_env() -> None:
    report = {"verdict": "blocked", "stages": []}
    findings = check_gauge(report)
    assert any(f.suggested_fix_class == "RETRY_ENV" for f in findings)


def test_attribute_directed_feedback() -> None:
    fb = attribute(
        [
            Finding(
                code="MISSING_RAIL",
                where=Where(net="5V"),
                evidence="轨 5V 未绑定",
                suggested_fix_class="REBIND_NET",
            )
        ]
    )
    assert "MISSING_RAIL@5V" in fb and "REBIND_NET" not in fb
    assert "补进" in fb


class _FakeAdapter:
    def __init__(self, gate_verdict: str) -> None:
        self.gate_verdict = gate_verdict
        self.calls: list[list[str]] = []

    def run(self, args):
        self.calls.append(args)
        if len(args) > 1 and args[1] == "block-apply":
            # 变更命令只执行一次:manifest 由本次 stdout 承载(controller 不重发)
            return 0, json.dumps({"ok": "applied", "placed": []}), ""
        return 0, "{}", ""

    def clear_all_pages(self):
        self.calls.append(["sch", "clear", "--all-windows"])

    def refresh_window(self):
        pass

    @property
    def window_id(self):
        return "fake"

    def run_json(self, args):
        self.calls.append(args)
        if args[1] == "gate":
            return {"verdict": self.gate_verdict, "stages": []}
        return {"ok": "applied"}

    def delete_primitives(self, ids):
        return {"ok": True}


_UP_WIDE = UpstreamRef(
    id="block.vehicle_input_tps54360_5v",
    ports={"IGN_12V": "IGN_12V", "VBAT_RAW": "VBAT_RAW", "5V": "+5V", "GND": "GND"},
)


def _catalog() -> dict[str, BlockRecord]:
    return {
        "ldo-ams1117-3v3": BlockRecord(
            block_id="ldo-ams1117-3v3",
            name="LDO",
            desc="x",
            category="power",
            upstream=UpstreamRef(
                id="block.ams1117_ldo_3v3", ports={"VIN_5V": "+5V", "3V3": "+3V3", "GND": "GND"}
            ),
        ),
        "dc-terminal-wide-input": BlockRecord(
            block_id="dc-terminal-wide-input",
            name="宽压输入",
            desc="x",
            upstream=_UP_WIDE,
        ),
    }


def _candidates() -> list[RetrievedBlock]:
    return [
        RetrievedBlock(
            block_id="ldo-ams1117-3v3",
            name="LDO",
            desc="x",
            category="power",
            tags=[],
            parts=[],
            ports=[],
            provenance="",
            upstream=UpstreamRef(
                id="block.ams1117_ldo_3v3", ports={"VIN_5V": "+5V", "3V3": "+3V3", "GND": "GND"}
            ),
            score=1.0,
            channels=["dense"],
        ),
        RetrievedBlock(
            block_id="dc-terminal-wide-input",
            name="宽压输入",
            desc="x",
            category="power",
            tags=[],
            parts=[],
            ports=[],
            provenance="",
            upstream=_UP_WIDE,
            score=0.9,
            channels=["dense"],
        ),
    ]


_PLAN_OK = {
    "blocks": [
        {
            "block_id": "ldo-ams1117-3v3",
            "upstream_id": "block.ams1117_ldo_3v3",
            "instance": "ldo1",
            "ports_binding": {"VIN_5V": "5V", "3V3": "3V3", "GND": "GND"},
        }
    ],
    "nets": [],
    "uncovered": [],
    "confidence": 0.9,
    "provenance": [],
}


def _loop(chat, adapter, ir=None, tmp="runs/test-loop") -> LoopController:
    ir = ir or _ir_with_rails(("3V3", 3.3), ("5V", 5.0))
    return LoopController(
        ir,
        _catalog(),
        lambda q: _candidates(),
        chat,
        adapter,
        AuditLog(tmp),
    )


def test_loop_converges_round1(tmp_path) -> None:
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    lc = _loop(chat, _FakeAdapter("pass"), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    assert result.converged_round == 1


def _ir_loop() -> DesignIR:
    return _ir_with_rails(("3V3", 3.3), ("5V", 5.0), ("12V", 12.0))


def test_loop_iterates_then_converges(tmp_path) -> None:
    good = json.loads(json.dumps(_PLAN_OK))
    good["blocks"].append(
        {
            "block_id": "dc-terminal-wide-input",
            "upstream_id": "block.vehicle_input_tps54360_5v",
            "instance": "dcin1",
            "ports_binding": {"VBAT_RAW": "12V"},
        }
    )
    replies = [json.dumps(_PLAN_OK, ensure_ascii=False), json.dumps(good, ensure_ascii=False)]

    class SeqChat:
        def chat(self, messages, *, model=None):
            return replies.pop(0)

    lc = LoopController(
        _ir_loop(),
        _catalog(),
        lambda q: _candidates(),
        SeqChat(),
        _FakeAdapter("pass"),
        AuditLog(str(tmp_path)),
    )
    result = lc.run()
    assert result.status == "PASS"
    assert result.converged_round == 2
    assert "MISSING_RAIL" in result.rounds[0].feedback


def test_loop_halts_on_oscillation(tmp_path) -> None:
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    lc = LoopController(
        _ir_loop(),
        _catalog(),
        lambda q: _candidates(),
        chat,
        _FakeAdapter("pass"),
        AuditLog(str(tmp_path)),
    )
    result = lc.run()
    assert result.status == "HALT"
    assert len(result.rounds) == 2
    assert "同错" in result.rounds[1].halted


def test_loop_gate_fail_blocks(tmp_path) -> None:
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    lc = LoopController(
        _ir_loop(),
        _catalog(),
        lambda q: _candidates(),
        chat,
        _FakeAdapter("fail"),
        AuditLog(str(tmp_path)),
    )
    result = lc.run()
    assert result.status == "HALT"
    assert any(f.code == "GATE_FAIL" for f in result.rounds[0].findings if not f.weak)


# ---- P4-1②:功能分区编排(zones set/zone-plan/zone-draw/note,EDALOOP_ZONES 门控) ----


class _ZoneFakeAdapter(_FakeAdapter):
    """block-apply manifest 带 placed 位号,sch pages/page-new 仿真;记录全部调用供断言。"""

    def run(self, args):
        self.calls.append(args)
        if args[1] == "block-apply":
            return 0, json.dumps({"ok": "applied", "placed": [{"designator": "U1"}, {"designator": "C1"}]}), ""
        return 0, "{}", ""

    def run_json(self, args):
        self.calls.append(args)
        if args[1] == "gate":
            return {"verdict": self.gate_verdict, "stages": []}
        if args[1] == "pages":
            return {"result": {"pages": [{"name": "P1", "uuid": "u1", "parentSchematicUuid": "s1"}]}}
        if args[1] == "page-new":
            return {"result": {"pageUuid": f"pg-{len(self.calls)}"}}
        return {"ok": "applied"}


def _zones_calls(adapter) -> dict[str, list]:
    out: dict[str, list] = {}
    for c in adapter.calls:
        if c and c[0] == "sch" and c[1] in ("zones", "zone-draw", "zone-plan", "zone-arrange", "note"):
            out.setdefault(c[1], []).append(c)
    return out


class _ZoneArrangeFakeAdapter(_ZoneFakeAdapter):
    """zone-plan 首报 partitionOverlap=2(两区互压),zone-arrange --apply 后清零。"""

    def __init__(self, gate_verdict: str) -> None:
        super().__init__(gate_verdict)
        self.plans = 0

    def run(self, args):
        if args[1] == "zone-arrange":
            self.calls.append(args)
            return 0, "verdict: pass", ""
        return super().run(args)

    def run_json(self, args):
        if args[1] == "zone-plan":
            self.calls.append(args)
            self.plans += 1
            overlap = 2 if self.plans == 1 else 0
            return {
                "validation": {
                    "sheetOverflow": 0,
                    "partitionOverlap": overlap,
                    "titleBlockHits": 0,
                    "moduleOutsideZone": 0,
                    "labelCollisions": 0,
                    "sheetMarginHits": 0,
                },
                "partitions": [{"name": "PWR"}, {"name": "MCU"}],
            }
        return super().run_json(args)


def test_zone_frames_off_by_default(tmp_path) -> None:
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _ZoneFakeAdapter("pass")
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    assert _zones_calls(adapter) == {}  # 未开 EDALOOP_ZONES 不碰分区命令


def test_zone_frames_sequence_when_enabled(tmp_path) -> None:
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _ZoneFakeAdapter("pass")
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    lc.zones_enabled = True
    result = lc.run()
    assert result.status == "PASS"
    calls = _zones_calls(adapter)
    assert set(calls) == {"zones", "zone-plan", "zone-draw", "note"}
    # zones clear 在 set 前
    flat = adapter.calls
    clear_i = next(i for i, c in enumerate(flat) if c[:3] == ["sch", "zones", "clear"])
    set_i = next(i for i, c in enumerate(flat) if c[:3] == ["sch", "zones", "set"])
    assert clear_i < set_i
    # 声明带真实位号:PWR=left:U1,C1(_PLAN_OK 只有 LDO,电源带)
    set_call = calls["zones"][1]
    assert "PWR=left:U1,C1" in set_call
    # 分区框用 partition 模式;注记挂靠 PWR 且在 gate 之前执行
    assert calls["zone-draw"][0][2:4] == ["--mode", "partition"]
    note_call = calls["note"][0]
    assert note_call[note_call.index("--zone") + 1] == "PWR"
    gate_i = next(i for i, c in enumerate(flat) if c[:2] == ["sch", "gate"])
    assert set_i < gate_i and next(i for i, c in enumerate(flat) if c[:2] == ["sch", "note"]) < gate_i


def test_zone_arrange_repairs_partition_overlap(tmp_path) -> None:
    """zone-plan 报可重排违规(两区体积互压)→ zone-arrange --apply 修复 →
    重 plan 确认 → zone-draw 才画(run8 残留:4/6 页 zone-draw rc=1 没框)。"""
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _ZoneArrangeFakeAdapter("pass")
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    lc.zones_enabled = True
    result = lc.run()
    assert result.status == "PASS"
    seq = [
        c[1] for c in adapter.calls if c and c[0] == "sch" and c[1] in ("zone-plan", "zone-arrange", "zone-draw")
    ]
    assert seq == ["zone-plan", "zone-arrange", "zone-plan", "zone-draw"]
    za = adapter.calls[[i for i, c in enumerate(adapter.calls) if c[1] == "zone-arrange"][0]]
    assert za[2] == "--apply"


# ---- P4-b2:多页编排(页流超 A4 → 建页 + --doc 钉扎 + 逐页 gate/zones) ----

_PLAN_2WIDE = {
    "blocks": [
        {
            "block_id": "dc-terminal-wide-input",
            "upstream_id": "block.vehicle_input_tps54360_5v",
            "instance": "dcin1",
            "ports_binding": {"VBAT_RAW": "12V"},
        },
        {
            "block_id": "dc-terminal-wide-input",
            "upstream_id": "block.vehicle_input_tps54360_5v",
            "instance": "dcin2",
            "ports_binding": {"VBAT_RAW": "12V"},
        },
    ],
    "nets": [],
    "uncovered": [],
    "confidence": 0.9,
    "provenance": [],
}


def test_multipage_orchestration(tmp_path) -> None:
    """宽压块(dy 1384)×2:单页放不下 → 建页 P2 + 落图 --doc 钉扎 + 逐页 gate/zones。"""
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _ZoneFakeAdapter("pass")
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    lc.zones_enabled = True
    result = lc.run()
    assert result.status == "PASS"
    flat = adapter.calls
    # 1) 建页:sch pages 读现状 → page-new(无名)→ page-rename --name P2
    pages_i = next(i for i, c in enumerate(flat) if c[:2] == ["sch", "pages"])
    new_i = next(i for i, c in enumerate(flat) if c[:2] == ["sch", "page-new"])
    rename_i = next(i for i, c in enumerate(flat) if c[:2] == ["sch", "page-rename"])
    assert pages_i < new_i < rename_i
    assert "--name" in flat[rename_i] and flat[rename_i][flat[rename_i].index("--name") + 1] == "P2"
    # 1b) 每轮全量清页:既有页 ∪ 计划页逐页 sch clear --doc(含 P1;前台粘滞使
    #     clear_all_pages 只清得到活动页,r≥2 不显式清 P1 会叠上轮墨迹 → 位号冲突)
    clears = [c for c in flat if c[:3] == ["sch", "clear", "--doc"]]
    assert sorted(c[-1] for c in clears) == ["P1", "P2"]
    # 2) 两块落图都 --doc 钉扎(P1 不豁免:--doc 切换粘性,免钉会落错页);
    #    manifest 单次执行路径只经 run() 记录(重发重放=孪生部件,见回归测试)
    raw_applies = [c for c in flat if c[:2] == ["sch", "block-apply"]]
    applies = [c for i, c in enumerate(raw_applies) if i == 0 or raw_applies[i - 1] != c]
    assert len(applies) == 2
    assert applies[0][applies[0].index("--doc") + 1] == "P1"
    assert applies[1][applies[1].index("--doc") + 1] == "P2"
    # 3) 逐页 gate:--doc P1 与 P2 各一次;zones 也逐页(set 带 --doc)
    gates = [c for c in flat if c[:2] == ["sch", "gate"]]
    assert sorted(c[c.index("--doc") + 1] for c in gates) == ["P1", "P2"]
    zone_sets = [c for c in flat if c[:3] == ["sch", "zones", "set"]]
    assert len(zone_sets) == 2
    assert all("--doc" in c for c in zone_sets)


# ---- P4-b2 续:清页保真(clear 三态结果 + 回读复核 + settle 重清) ----


class _GhostInkAdapter(_ZoneFakeAdapter):
    """仿真 settle 电阻:P1 首趟 clear 后回读仍有 2 器件,第二趟才真清空。"""

    def __init__(self, gate_verdict: str) -> None:
        super().__init__(gate_verdict)
        self.dirty_reads = 0

    def run_json(self, args):
        if args[1] == "read":
            self.calls.append(args)
            self.dirty_reads += 1
            if self.dirty_reads == 1:  # 首次回读:幽灵墨迹在场
                return {
                    "result": {
                        "components": [
                            {"componentType": "part", "designator": "U9"},
                            {"componentType": "part", "designator": "C9"},
                        ]
                    }
                }
            return {"result": {"components": [{"componentType": "sheet"}]}}
        return super().run_json(args)


def test_clear_fidelity_verify_and_retry(tmp_path) -> None:
    """首趟 clear 后回读有 survivors → 自动重清 → 第二趟干净;
    审计 page-clear-doc 带 survivors/attempt/ok 双趟记录。"""
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _GhostInkAdapter("pass")
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    # P1 被清了两趟(首趟回读见 2 器件 → settle 重清)
    clears = [c for c in adapter.calls if c[:3] == ["sch", "clear", "--doc"]]
    assert [c[-1] for c in clears] == ["P1", "P1"]
    # 审计:两趟 page-clear-doc,首趟 ok=False(survivors=2),次趟 ok=True
    events = [json.loads(line) for line in Path(str(tmp_path), "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    pcd = [e for e in events if e.get("kind") == "page-clear-doc"]
    assert [(e["attempt"], e["survivors"], e["ok"]) for e in pcd] == [(1, 2, False), (2, 0, True)]
    # page-clear 汇总:failures 清零(重清成功)
    pc = [e for e in events if e.get("kind") == "page-clear"]
    assert pc[-1]["failures"] == []


def test_block_apply_executes_once(tmp_path) -> None:
    """回归(run3b 六连挂根因):block-apply manifest 从首次执行的 stdout 解析,
    同参命令绝不被重发——重放=同页孪生部件再放一份(平台分号 D4/J2/R3…、
    几何逐位相同),apply 内置 verify 逐件报 overlap+pin coincidence 判死,
    第一遍成品被误记 failed-rolled-back。"""
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _ZoneFakeAdapter("pass")
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    # 修复前:adapter.run + _run_json_retry(args) 各发一遍 = 2
    runs = [c for c in adapter.calls if c[:2] == ["sch", "block-apply"]]
    assert len(runs) == 1


# ---- P4-b3:布局收口(clusters ERROR 页拆组重排)+ 明细表补齐 ----


class _ArrangeFakeAdapter(_ZoneFakeAdapter):
    """clusters 首查 ERROR(块内 D3↔J1 + 跨块 J1↔R1),第 dirty_arranges 次
    group-arrange 后清零;group list:g1=问题块(D3,J1,D1,R9)、g2=R1 所在块;
    block-apply manifest 位号与 g1 对齐(placed_by_page 命中路径)。
    arrange_rc=1 仿真「装不下拒排」(fit 类),=0 仿真已执行。"""

    def __init__(
        self,
        gate_verdict: str,
        dirty_arranges: int = 1,
        arrange_rc: int = 0,
        placed: tuple[str, ...] = ("D3", "J1", "D1", "R9"),
        errors: tuple[tuple[str, str, str], ...] = (("overlap", "D3", "J1"), ("overlap", "J1", "R1")),
        clamp_clean: bool = False,
        extra_clusters: dict[str, dict] | None = None,
        refuse_first: int = 0,
    ) -> None:
        super().__init__(gate_verdict)
        self.arranges = 0
        self.dirty_arranges = dirty_arranges
        self.arrange_rc = arrange_rc
        self.placed = list(placed)
        self.errors = list(errors)
        self.clamp_clean = clamp_clean
        self.extra_clusters = extra_clusters or {}
        self.clamps = 0
        self.ok_moves = 0
        self.refuse_first = refuse_first

    def run(self, args):
        if args[1] == "block-apply":
            self.calls.append(args)
            return 0, json.dumps(
                {"ok": "applied", "placed": [{"designator": d} for d in self.placed]}
            ), ""
        if args[1] == "clusters":
            self.calls.append(args)
            dirty = self.ok_moves < 1 if self.clamp_clean else self.arranges < self.dirty_arranges
            if dirty:
                findings = [
                    {"type": t, "a": a, "b": b or None, "level": "ERROR"} for t, a, b in self.errors
                ]
                clusters = []
                for t, a, b in self.errors:
                    box = {"minX": 1200.0, "minY": 600.0, "maxX": 1300.0, "maxY": 700.0}
                    clusters.append({"designator": a, "primitiveId": f"p-{a}", "box": box})
                    if b:
                        clusters.append({"designator": b, "primitiveId": f"p-{b}", "box": dict(box)})
                for d, box in self.extra_clusters.items():
                    clusters.append({"designator": d, "primitiveId": f"p-{d}", "box": dict(box)})
                return (
                    1,
                    json.dumps(
                        {
                            "findings": findings,
                            "clusters": clusters,
                            "sheetUsable": {"minX": 12, "minY": 12, "maxX": 1158, "maxY": 813},
                        }
                    ),
                    "",
                )
            return 0, json.dumps({"findings": []}), ""
        if args[:2] == ["sch", "group-move"]:
            self.calls.append(args)
            self.clamps += 1
            if self.clamps <= self.refuse_first:  # 内核拒移(keepout 钳 Δ0 / 共享连线树)
                return 1, "appliedΔ=(0,-0) group-move 未执行", ""
            self.ok_moves += 1
            return 0, "moved", ""
        if args[:3] == ["sch", "group", "list"]:
            self.calls.append(args)
            g1_members = [d for d in self.placed if d != "MCUSTM32"]
            return (
                0,
                json.dumps(
                    {
                        "groupsByPage": {
                            "u1": [
                                {"id": "g1", "members": [{"designator": d} for d in g1_members]},
                                {"id": "g2", "members": [{"designator": "R1"}]},
                            ]
                        }
                    }
                ),
                "",
            )
        if args[1] == "group-arrange":
            self.calls.append(args)
            self.arranges += 1
            return self.arrange_rc, "arranged", ""
        return super().run(args)


def test_arrange_closeout_shatters_named_culprits_only(tmp_path) -> None:
    """ERROR 点名谁拆谁:D3/J1 单件化 + 无辜件(D1,R9)重封一组保持刚体;
    跨块命中的 R1 所在组(g2)也点名拆;group-arrange --annotate=false
    --gap 80 后清零不升档;全链先于 gate。"""
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _ArrangeFakeAdapter("pass")
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    flat = adapter.calls
    assert [c[c.index("--group") + 1] for c in flat if c[:3] == ["sch", "group", "ungroup"]] == ["g1", "g2"]
    creates = [c[c.index("--members") + 1] for c in flat if c[:4] == ["sch", "group", "create", "--members"]]
    assert sorted(creates) == ["D1,R9", "D3", "J1", "R1"]  # 2 点名单件 + 1 余件组 + 跨块 R1
    arr = [c for c in flat if c[:2] == ["sch", "group-arrange"]]
    assert len(arr) == 1
    assert arr[0][arr[0].index("--gap") + 1] == "80"
    assert "--annotate=false" in arr[0]  # 分区框/注记归 zones 管,arrange 不得重复画
    gate_i = next(i for i, c in enumerate(flat) if c[:2] == ["sch", "gate"])
    ung_i = next(i for i, c in enumerate(flat) if c[:3] == ["sch", "group", "ungroup"])
    assert ung_i < gate_i


def test_arrange_closeout_escalates_gap(tmp_path) -> None:
    """gap 80 已执行(rc=0)仍脏 → 微叠类,升 140 重排一次。"""
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _ArrangeFakeAdapter("pass", dirty_arranges=2)
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    arr = [c for c in adapter.calls if c[:2] == ["sch", "group-arrange"]]
    assert [c[c.index("--gap") + 1] for c in arr] == ["80", "140"]


def test_arrange_closeout_downsizes_gap_on_fit_refusal(tmp_path) -> None:
    """拒排(rc=1 装不下,run5 P1/P5 类)→ 梯子向下:80 拒排换 60 硬塞,
    绝不升 140(更大 gap 只会更装不下)。"""
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _ArrangeFakeAdapter("pass", dirty_arranges=2, arrange_rc=1)
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    arr = [c for c in adapter.calls if c[:2] == ["sch", "group-arrange"]]
    assert [c[c.index("--gap") + 1] for c in arr] == ["80", "60"]


def test_arrange_closeout_tries_gap_40_before_split(tmp_path) -> None:
    """拒排且 60 仍拒(run6 P2 实测总需仅超带 4~24 单位)→ 40 档补刀,
    不必动组结构。"""
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _ArrangeFakeAdapter("pass", dirty_arranges=3, arrange_rc=1)
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    arr = [c for c in adapter.calls if c[:2] == ["sch", "group-arrange"]]
    assert [c[c.index("--gap") + 1] for c in arr] == ["80", "60", "40"]


def test_arrange_closeout_clamps_strays_when_arrange_refuses(tmp_path) -> None:
    """梯子 80/60/40 全拒(arrange 的组占地含挂线,拆组不缩翼展——run6 P1
    现场实验 7 单件仍拒)→ 钳回兜底:out-of-sheet 点名件按 sheetUsable(比
    arrange 带宽,含图签带)刚移回界内,snap-5 远离零取整。"""
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _ArrangeFakeAdapter(
        "pass",
        dirty_arranges=99,
        arrange_rc=1,
        placed=("D3", "J1", "R3", "R9"),
        errors=(("out-of-sheet", "R3", ""),),
        clamp_clean=True,
    )
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    arr = [c for c in adapter.calls if c[:2] == ["sch", "group-arrange"]]
    assert [c[c.index("--gap") + 1] for c in arr] == ["80", "60", "40"]
    mv = [c for c in adapter.calls if c[:2] == ["sch", "group-move"]]
    # R3 在 g1(placed 去 MCUSTM32);box maxX=1300 越界 142 → snap-5 远离零 = -145
    assert len(mv) == 1 and mv[0][mv[0].index("--group") + 1] == "g1"
    assert mv[0][mv[0].index("--dx") + 1] == "-145" and mv[0][mv[0].index("--dy") + 1] == "0"
    events = [json.loads(line) for line in Path(str(tmp_path), "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    clamp = [e for e in events if e.get("kind") == "arrange-clamp"]
    assert [(e["designator"], e["dx"], e["dy"]) for e in clamp] == [("R3", -145, 0)]
    assert [e["remaining"] for e in events if e.get("kind") == "arrange-result"] == [0]


def test_arrange_closeout_clamp_prefers_own_zone(tmp_path) -> None:
    """钳回落点优先本 zone 包络(run7 残留 partitionOverlap=3 的根因:裸钳把
    件甩进邻居分区):多个空位可行时取离本 zone 其他成员联合包络中心最近
    的落位,而不是最小位移的第一个空位。无 zone 认领时保持最小位移。"""
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _ArrangeFakeAdapter(
        "pass",
        dirty_arranges=99,
        arrange_rc=1,
        placed=("D3", "J1", "R3", "R9"),  # 同一块 → 同认领 PWR
        errors=(("out-of-sheet", "R3", ""),),
        clamp_clean=True,
        extra_clusters={  # PWR 其余成员的箱(zone 包络 100,100-600,200)
            "D3": {"minX": 100.0, "minY": 100.0, "maxX": 200.0, "maxY": 200.0},
            "J1": {"minX": 300.0, "minY": 100.0, "maxX": 400.0, "maxY": 200.0},
            "R9": {"minX": 500.0, "minY": 100.0, "maxX": 600.0, "maxY": 200.0},
        },
    )
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    mv = [c for c in adapter.calls if c[:2] == ["sch", "group-move"]]
    # 6 个扫描空位(s=0..5 → dx -145..-445)全部可行且都远离成员箱;
    # zone 中心 x=350 → 最近 = 最深一步 dx=-445(无 zone 时应取 s=0 的 -145)
    assert mv and mv[0][mv[0].index("--dx") + 1] == "-445"


def test_arrange_closeout_never_repeats_refused_clamp(tmp_path) -> None:
    """内核拒过的位移绝不重发(run8 P1/P2 实证:同 finding 反复推导同一被拒
    下推,4 次空转):首选 b 下推被拒 → 换下一候选(a 下推)而不是原地复读。"""
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _ArrangeFakeAdapter(
        "pass",
        dirty_arranges=99,
        arrange_rc=1,
        placed=("D3", "J2", "D1", "R9"),
        errors=(("overlap", "D3", "J2"),),
        clamp_clean=True,
        refuse_first=1,  # 第一发(J2 下推)被内核拒
    )
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    mv = [c for c in adapter.calls if c[:2] == ["sch", "group-move"]]
    # 候选序:J2 下推(-140) 被拒 → D3 下推(-140);同位移不同件不混淆
    assert [(c[c.index("--dx") + 1], c[c.index("--dy") + 1]) for c in mv] == [("0", "-140"), ("0", "-140")]
    events = [json.loads(line) for line in Path(str(tmp_path), "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    clamp = [e for e in events if e.get("kind") == "arrange-clamp"]
    assert [(e["designator"], e["rc"]) for e in clamp] == [("J2", 1), ("D3", 0)]
    assert [e["remaining"] for e in events if e.get("kind") == "arrange-result"] == [0]


def test_arrange_closeout_separates_overlaps_when_arrange_refuses(tmp_path) -> None:
    """拒排页的 overlap(双方都在带内,钳回不动它)→ b 沿 y 下推 40 分离
    (下方不够再上推;再无解归反馈域)。"""
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _ArrangeFakeAdapter(
        "pass",
        dirty_arranges=99,
        arrange_rc=1,
        placed=("D3", "J2", "D1", "R9"),
        errors=(("overlap", "D3", "J2"),),
        clamp_clean=True,
    )
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    mv = [c for c in adapter.calls if c[:2] == ["sch", "group-move"]]
    # D3 箱 (1200,600)-(1300,700),J2 同箱:down = 600-40-700 = -140
    assert len(mv) == 1 and mv[0][mv[0].index("--dy") + 1] == "-140"
    events = [json.loads(line) for line in Path(str(tmp_path), "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    clamp = [e for e in events if e.get("kind") == "arrange-clamp"]
    assert [(e["designator"], e["dy"]) for e in clamp] == [("J2", -140)]


def test_arrange_closeout_groups_place_channel_strays(tmp_path) -> None:
    """place 通道件(MCUSTM32)不在任何组里 → arrange 对它零手段(run5 P3/P4
    out-of-sheet 永存实证)→ 补建单件组纳入可动域。"""
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _ArrangeFakeAdapter(
        "pass",
        placed=("D3", "J1", "D1", "MCUSTM32"),
        errors=(("out-of-sheet", "MCUSTM32", ""), ("overlap", "D3", "J1")),
    )
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    flat = adapter.calls
    creates = [c[c.index("--members") + 1] for c in flat if c[:4] == ["sch", "group", "create", "--members"]]
    # g1 成员=placed 去掉散件;D3/J1 点名单件,D1 余件单件,MCUSTM32 补建单件
    assert sorted(creates) == ["D1", "D3", "J1", "MCUSTM32"]


def test_arrange_closeout_skips_clean_pages(tmp_path) -> None:
    """clusters 无 ERROR 的页绝不重排(干净几何重洗=收益为负);
    明细表仍逐页补(titleblock-get 无字段 → 只 --show,不盲写 --data)。"""
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _ZoneFakeAdapter("pass")  # clusters → "{}" 无 findings
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    assert not [c for c in adapter.calls if c[:2] == ["sch", "group-arrange"]]
    assert not [c for c in adapter.calls if c[:3] == ["sch", "group", "ungroup"]]
    shows = [c for c in adapter.calls if c[:2] == ["sch", "titleblock"]]
    assert shows and all("--doc" in c for c in shows)
    assert not [c for c in shows if "--data" in c]


class _TitleFakeAdapter(_ZoneFakeAdapter):
    def run(self, args):
        if args[1] == "titleblock-get":
            self.calls.append(args)
            return (
                0,
                json.dumps(
                    {"result": {"fields": {"Title": {"value": "old"}, "Size": {"value": "A4"}}}}
                ),
                "",
            )
        return super().run(args)


def test_titleblock_writes_discovered_key(tmp_path) -> None:
    """titleblock-get 探到 Title → --show + --data Title=<需求·页>;
    只写存在的 key(写不存在的 key 平台静默丢弃还 rc!=0)。"""
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _TitleFakeAdapter("pass")
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    data = [c for c in adapter.calls if "--data" in c]
    assert len(data) == 1
    payload = json.loads(data[0][data[0].index("--data") + 1])
    assert payload == {"Title": {"value": "t · P1"}}  # ir.source=t.md → stem t
    assert data[0][data[0].index("--doc") + 1] == "P1"


class _TitleRealShapeAdapter(_ZoneFakeAdapter):
    """真机形态(run7 实探):键表在 result.titleBlockData,Name 是标题格;
    写返回 rc=1 nothing-applied 假失败(写后即时回读撞图签重建 stale 窗口),
    但值已落——控制器按值回读判真伪。"""

    def __init__(self, gate_verdict: str) -> None:
        super().__init__(gate_verdict)
        self.tb: dict[str, dict] = {"Name": {"value": ""}, "Size": {"value": "A4"}}

    def run(self, args):
        if args[1] == "titleblock-get":
            self.calls.append(args)
            return 0, json.dumps({"result": {"titleBlockData": self.tb}}), ""
        if args[1] == "titleblock" and "--data" in args:
            self.calls.append(args)
            patch = json.loads(args[args.index("--data") + 1])
            for k, v in patch.items():
                self.tb[k] = {"value": v["value"]}
            return 1, "nothing was applied", ""
        return super().run(args)


def test_titleblock_real_shape_and_false_negative_verified(tmp_path) -> None:
    """键表在 titleBlockData(旧解析漏这层→key 恒 None);写 rc=1 假失败时
    按值回读判真伪(verified=True),不冤枉也不轻信。"""
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _TitleRealShapeAdapter("pass")
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    data = [c for c in adapter.calls if "--data" in c]
    assert len(data) == 1
    assert json.loads(data[0][data[0].index("--data") + 1]) == {"Name": {"value": "t · P1"}}
    events = [json.loads(line) for line in Path(str(tmp_path), "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    tb = [e for e in events if e.get("kind") == "titleblock"]
    assert [(e["key"], e["rc"], e["verified"]) for e in tb] == [("Name", 1, True)]
