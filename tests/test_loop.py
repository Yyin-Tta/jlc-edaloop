from __future__ import annotations

import json
import os
import re

import pytest
from pathlib import Path

from edaloop.generate.audit import AuditLog
from edaloop.generate.compile import compile_actions
from edaloop.generate.models import Action, BlockPlan
from edaloop.intent.ir import DesignIR
from edaloop.knowledge.models import BlockRecord, RetrievedBlock, UpstreamRef
from edaloop.llm.fake import FakeChat
from edaloop.loop.attribution import attribute
from edaloop.loop.controller import LoopController
from edaloop.validate.checks import (
    check_gauge,
    check_net_existence,
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


def test_check_net_existence_pure() -> None:
    """P0 net 存在性终检纯函数:逐页对照计划网与页内实际网;NC/空名豁免;
    actual 缺页 = 该页计划网全缺(供 controller 的 UNVERIFIED 门先行 continue)。"""
    planned = {"P1": {"3V3", "GND", "MCU_TX"}, "P2": {"5V", "GND", "NC", ""}}
    actual = {"P1": {"3V3", "GND", "MCU_TX", "EXTRA"}}  # P2 在 actual 里整页缺失
    out = check_net_existence(planned, actual)
    assert out == [{"page": "P2", "missing": ["5V", "GND"]}]  # NC 与空名不算缺失
    # 多网缺失按名排序稳定输出
    out2 = check_net_existence(
        {"P1": {"A", "B", "C"}}, {"P1": {"B"}})
    assert out2 == [{"page": "P1", "missing": ["A", "C"]}]
    # 计划空/全命中 → 空(全过)
    assert check_net_existence({}, {"P1": {"GND"}}) == []
    assert check_net_existence({"P1": {"GND"}}, {"P1": {"GND"}}) == []


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


def test_check_gate_overlap_evidence_names_parties() -> None:
    """G33:overlap evidence 必须点名双方(a/b 进 _compact 键表)——RELAYOUT
    反馈据此定位修复对象;不同双方不折叠成同一条 type=overlap。"""
    report = {
        "verdict": "fail",
        "stages": [
            {
                "stage": "clusters",
                "verdict": "fail",
                "findings": [
                    {"type": "overlap", "a": "R9", "b": "R10"},
                    {"type": "overlap", "a": "D2", "b": "J1"},
                ],
            }
        ],
    }
    findings = check_gauge(report)
    assert len(findings) == 2  # 双方不同 → 两条独立,不折叠
    r9 = next(f for f in findings if "R9" in f.evidence)
    assert "a=R9" in r9.evidence and "b=R10" in r9.evidence


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


# 真机给上游画布风暴留的块间歇排水口,单测关掉(否则 35 实例×2s 拖慢全量)
LoopController._MEASURE_PACE = 0.0


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

# 单块形态:断网计数 ≤3 的最小场景(1 内部网 × 2 脚 = 2 处)
_PLAN_1BLK = {
    "blocks": [
        {
            "block_id": "dc-terminal-wide-input",
            "upstream_id": "block.vehicle_input_tps54360_5v",
            "instance": "dcin1",
            "ports_binding": {"VBAT_RAW": "12V"},
        },
    ],
    "nets": [],
    "uncovered": [],
    "confidence": 0.9,
    "provenance": [],
}


def test_multipage_orchestration(tmp_path, monkeypatch) -> None:
    """宽压块(dy 1384)×2:单页放不下 → 建页 P2 + 落图 --doc 钉扎 + 逐页 gate/zones。
    (流式模式护栏:repack 两阶段编排另有专属测试。)"""
    monkeypatch.setenv("EDALOOP_LAYOUT", "flow")
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
    # P1 被清了四趟:repack 试放画布清残(P0-3)吃掉首趟幽灵(settle 重清),
    # 逐块量测后的清场一趟(落-量-清),之后 run() 的逐页清页再清一趟
    # (此时已干净,单趟即过)
    clears = [c for c in adapter.calls if c[:3] == ["sch", "clear", "--doc"]]
    assert [c[-1] for c in clears] == ["P1", "P1", "P1", "P1"]
    # 审计:page-clear-doc 四趟,首趟 ok=False(survivors=2)→ settle 重清 ok,
    # 量测清场与生产清页各单趟干净
    events = [json.loads(line) for line in Path(str(tmp_path), "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    pcd = [e for e in events if e.get("kind") == "page-clear-doc"]
    assert [(e["attempt"], e["survivors"], e["ok"]) for e in pcd] == [
        (1, 2, False), (2, 0, True), (1, 0, True), (1, 0, True)]
    # page-clear 汇总:failures 清零(重清成功)
    pc = [e for e in events if e.get("kind") == "page-clear"]
    assert pc[-1]["failures"] == []


def test_block_apply_executes_once(tmp_path, monkeypatch) -> None:
    """回归(run3b 六连挂根因):block-apply manifest 从首次执行的 stdout 解析,
    同参命令绝不被重发——重放=同页孪生部件再放一份(平台分号 D4/J2/R3…、
    几何逐位相同),apply 内置 verify 逐件报 overlap+pin coincidence 判死,
    第一遍成品被误记 failed-rolled-back。(repack 的试放/重放是不同页不同参,
    不属「重发」;此处在流式模式下守住单发纪律。)"""
    monkeypatch.setenv("EDALOOP_LAYOUT", "flow")
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

    _USABLE = {"minX": 12, "minY": 12, "maxX": 1158, "maxY": 813}

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
        refuse_until_arrange: bool = False,  # 分流首钳全程拒移(梯子跑过前的 group-move 一律 rc=1)
        sheet_usable: dict | None = _USABLE,  # 传 None → clusters 报告整体缺 sheetUsable(图框丢失现场)
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
        self.refuse_until_arrange = refuse_until_arrange
        self.disbanded: set[str] = set()  # 动态组语义(真 daemon:create/ungroup 对 list 可见)
        self.created: list[dict] = []
        self.sheet_usable = dict(sheet_usable) if sheet_usable is not None else None

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
                payload = {"findings": findings, "clusters": clusters}
                if self.sheet_usable is not None:
                    payload["sheetUsable"] = dict(self.sheet_usable)
                return 1, json.dumps(payload), ""
            return 0, json.dumps({"findings": []}), ""
        if args[:2] == ["sch", "group-move"]:
            self.calls.append(args)
            self.clamps += 1
            if self.clamps <= self.refuse_first:  # 内核拒移(keepout 钳 Δ0 / 共享连线树)
                return 1, "appliedΔ=(0,-0) group-move 未执行", ""
            if self.refuse_until_arrange and self.arranges == 0:
                return 1, "appliedΔ=(0,-0) group-move 未执行", ""
            self.ok_moves += 1
            return 0, "moved", ""
        if args[:3] == ["sch", "group", "list"]:
            self.calls.append(args)
            g1_members = [d for d in self.placed if d != "MCUSTM32"]
            base = [
                {"id": "g1", "members": [{"designator": d} for d in g1_members]},
                {"id": "g2", "members": [{"designator": "R1"}]},
            ]
            return (
                0,
                json.dumps(
                    {
                        "groupsByPage": {
                            "u1": [g for g in base if g["id"] not in self.disbanded]
                            + [dict(g) for g in self.created]
                        }
                    }
                ),
                "",
            )
        if args[:3] == ["sch", "group", "ungroup"]:
            self.calls.append(args)
            self.disbanded.add(args[args.index("--group") + 1])
            return 0, json.dumps({"ok": True}), ""
        if args[:3] == ["sch", "group", "create"]:
            self.calls.append(args)
            members = args[args.index("--members") + 1].split(",")
            self.created.append({
                "id": f"gc{len(self.created)}",
                "members": [{"designator": d} for d in members],
            })
            return 0, json.dumps({"ok": True}), ""
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
    # g2={R1} 已是单件组:拆了重建=同构换 id 空转,保持(v0.6.11);只有
    # 多成员的 g1(含 D3/J1 点名)解体重构
    assert [c[c.index("--group") + 1] for c in flat if c[:3] == ["sch", "group", "ungroup"]] == ["g1"]
    creates = [c[c.index("--members") + 1] for c in flat if c[:4] == ["sch", "group", "create", "--members"]]
    assert sorted(creates) == ["D1,R9", "D3", "J1"]  # 2 点名单件 + 1 余件组(R1 留在 g2)
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
    """v0.6.11 分流:收口先试最小干预钳移——首钳被内核拒移(keepout Δ0/
    共享连线树类)→ 梯子 80/60/40 全拒(arrange 的组占地含挂线,拆组不缩
    翼展——run6 P1 现场实验 7 单件仍拒)→ 末次钳回兜底:out-of-sheet 点名件
    按 sheetUsable(比 arrange 带宽,含图签带)刚移回界内,snap-5 远离零取整。"""
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _ArrangeFakeAdapter(
        "pass",
        dirty_arranges=99,
        arrange_rc=1,
        placed=("D3", "J1", "R3", "R9"),
        errors=(("out-of-sheet", "R3", ""),),
        clamp_clean=True,
        refuse_until_arrange=True,
    )
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    arr = [c for c in adapter.calls if c[:2] == ["sch", "group-arrange"]]
    assert [c[c.index("--gap") + 1] for c in arr] == ["80", "60", "40"]
    mv = [c for c in adapter.calls if c[:2] == ["sch", "group-move"]]
    # R3 在 g1(placed 去 MCUSTM32);box maxX=1300 越界 142 → snap-5 远离零 = -145。
    # 分流首钳的候选(钳回轴 + 60 步避邻居扫描)全被内核拒 → 梯子全拒(g1 已
    # 拆组重构,R3 单件组 gc0)→ 末次钳(新 spent 集)重发首选 rc=0 清零
    assert len(mv) >= 2
    assert all(m[m.index("--dx") + 1] == "-145" for m in mv)
    assert all(m[m.index("--group") + 1] == "g1" for m in mv[:-1])
    assert mv[-1][mv[-1].index("--group") + 1] == "gc0"  # 拆组后 R3 的单件组
    assert all(m[m.index("--dx") + 1] == "-145" for m in mv)
    assert mv[-1][mv[-1].index("--dy") + 1] == "0"
    events = [json.loads(line) for line in Path(str(tmp_path), "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    clamp = [e for e in events if e.get("kind") == "arrange-clamp"]
    assert all(e["designator"] == "R3" and e["dx"] == -145 for e in clamp)
    assert clamp[-1]["dy"] == 0 and clamp[-1]["rc"] == 0
    assert all(e["rc"] == 1 for e in clamp[:-1])
    assert [e["remaining"] for e in events if e.get("kind") == "arrange-result"] == [0]


def test_clamp_falls_back_to_contract_band_when_usable_missing(tmp_path) -> None:
    """clusters 报告整体缺 sheetUsable(图框几何丢失现场,req-01 daily HALT 链:
    上游拒排 + 旧代码静默零修复)→ 回退 planner 契约带继续刚移并留痕:
    R3 maxX=1300 越回退带 maxX=1100 → dx=-200(真带 1158 时是 -145)。"""
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _ArrangeFakeAdapter(
        "pass",
        dirty_arranges=99,
        arrange_rc=1,
        placed=("D3", "J1", "R3", "R9"),
        errors=(("out-of-sheet", "R3", ""),),
        clamp_clean=True,
        sheet_usable=None,
    )
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    mv = [c for c in adapter.calls if c[:2] == ["sch", "group-move"]]
    assert len(mv) == 1 and mv[0][mv[0].index("--dx") + 1] == "-200"
    assert mv[0][mv[0].index("--dy") + 1] == "0"
    events = [json.loads(line) for line in Path(str(tmp_path), "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e.get("kind") == "clamp-band-fallback" for e in events)
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
    # 8 级主轴扫描(s=0..7 → dx -145..-565)全部可行且都远离成员箱;
    # zone 中心 x=350 → 最近 = 最深一步 dx=-565(无 zone 时应取 s=0 的 -145;
    # 2D 扩grid后梯子 6→8 级,最近可用从 -445 变 -565,断言随实况更新)
    assert mv and mv[0][mv[0].index("--dx") + 1] == "-565"


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
    明细表仍逐页只读保位(--show,永不 --data 写入)。"""
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


def test_titleblock_never_writes_data(tmp_path) -> None:
    """0.26.0 明令 titleblock --data 写入禁令(该写路径触发图签重建、损毁
    sheet 符号引用 → 重启后图框丢失 → group-arrange 拒排 → HALT,req-01
    daily 定案):主链只许 --show 保可见性,全链不许出现 --data 与
    titleblock-get。"""
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _ZoneFakeAdapter("pass")
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    shows = [c for c in adapter.calls if c[:2] == ["sch", "titleblock"]]
    assert shows and all("--show" in c and "--doc" in c for c in shows)
    assert not [c for c in adapter.calls if "--data" in c]
    assert not [c for c in adapter.calls if c[1] == "titleblock-get"]


def test_titleblock_show_audited_per_page(tmp_path) -> None:
    """只读探测逐页留痕(kind=titleblock:page/show_rc),失败只审计不判负
    (注释类非致命,不因图签问题拖垮收敛)。"""
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _ZoneFakeAdapter("pass")
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    assert lc.run().status == "PASS"
    events = [json.loads(line) for line in Path(str(tmp_path), "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    tb = [e for e in events if e.get("kind") == "titleblock"]
    assert [(e["page"], e["show_rc"]) for e in tb] == [("P1", 0)]


def test_run_version_gate_before_mutation(tmp_path) -> None:
    """P5-0 版本门前移:真机 run 主链首轮变更前必须过版本门(此前仅 apply 查)。

    适配器带 check_version 且 raise → run() 在任何变更命令前炸出;
    旧钉扎形态(0.25.1)正是本机实装 1.1.1 时的漂移场景。
    """
    from edaloop.generate.adapter import AdapterError

    class _VersionGateAdapter(_FakeAdapter):
        def check_version(self) -> str:
            raise AdapterError("easyeda-agent 版本 0.25.1 与钉死版本 1.1.1 不一致(ADR-0002)")

    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _VersionGateAdapter("pass")
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    with pytest.raises(AdapterError, match="ADR-0002"):
        lc.run()
    # 门在任何落图命令(block-apply/clear)之前炸出
    mutating = [c for c in adapter.calls if len(c) > 1 and c[1] in ("block-apply",) or c[:2] == ["sch", "clear"]]
    assert mutating == []


def test_run_no_check_version_method_skips_gate(tmp_path) -> None:
    """无 check_version 的适配器(Fake/测试形态)不受版本门影响,照常收敛。"""
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    lc = _loop(chat, _FakeAdapter("pass"), tmp=str(tmp_path))
    assert lc.run().status == "PASS"


# ---- P5-0 页修剪:计划外 P\d+ 孤儿页删除(netlist 导出页数超载的根因修复) ----


class _PruneFakeAdapter(_ZoneFakeAdapter):
    """sch pages 返回 P1..P5+Main(非 harness 命名);page-delete 记录入 calls 供断言。"""

    def run_json(self, args):
        if args[1] == "pages":
            return {"result": {"pages": [
                {"name": "P1", "uuid": "u1", "parentSchematicUuid": "s1"},
                {"name": "P2", "uuid": "u2", "parentSchematicUuid": "s1"},
                {"name": "P3", "uuid": "u3", "parentSchematicUuid": "s1"},
                {"name": "P4", "uuid": "u4", "parentSchematicUuid": "s1"},
                {"name": "P5", "uuid": "u5", "parentSchematicUuid": "s1"},
                {"name": "Main", "uuid": "um", "parentSchematicUuid": "s1"},
            ]}}
        return super().run_json(args)


def test_page_prune_deletes_orphan_plan_pages(tmp_path) -> None:
    chat = FakeChat(json.dumps(_PLAN_OK, ensure_ascii=False))
    adapter = _PruneFakeAdapter("pass")
    lc = _loop(chat, adapter, tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    dels = [c for c in adapter.calls if c[:2] == ["sch", "page-delete"]]
    # 计划只落 P1 → P2..P5 删(uuid 断言),Main 非 ^P\d+$ 不动
    assert sorted(c[3] for c in dels) == ["u2", "u3", "u4", "u5"]
    assert not any("um" in c for c in dels)
    # 清内容只剩 P1+Main(删除的页不再逐页 clear)
    cleared = [c for c in adapter.calls if c[:2] == ["sch", "clear"]]
    docs = {c[i + 1] for c in cleared for i, a in enumerate(c) if a == "--doc"}
    assert sorted(docs) == ["Main", "P1"]


# ---- 位号避撞(req-07 r2 实证):block-apply 子件占号 → std place 换空号 + autoconnect 同步改写 ----


class _DesigFakeAdapter(_ZoneFakeAdapter):
    """block-apply manifest 落 C1(vehicle 子件占号段缩影);place 回执回显请求位号。"""

    def __init__(self, gate_verdict: str) -> None:
        super().__init__(gate_verdict)
        self.last_place = ""

    def run(self, args):
        self.calls.append(args)
        if args[1] == "block-apply":
            return 0, json.dumps({"ok": "applied", "placed": [{"designator": "C1"}]}), ""
        return 0, "{}", ""

    def run_json(self, args):
        self.calls.append(args)
        if args[1] == "place":
            d = args[args.index("--designator") + 1]
            self.last_place = d
            return {"result": {"component": {"designator": d}}}
        if args[1] == "read":
            return {"result": {"components": [
                {"designator": self.last_place,
                 "pins": [{"number": "1", "name": "A"}, {"number": "2", "name": "B"}]}
            ]}}
        if args[0] == "lib":
            return {"result": {"components": [{"libraryUuid": "L1", "uuid": "V1"}]}}
        return super().run_json(args)


def test_apply_renames_colliding_designator(tmp_path) -> None:
    """std 件请求 C1 时 C1 已被 block-apply 子件占用 → place 换 C2(autoconnect 引用同步)。

    机制背景:EasyEDA 注解引擎对全项目重复位号异步改号且 place 回执照抄请求名,
    活态(actual)与请求(requested)分裂 → autoconnect 按请求名找不到件 rc≠0
    (req-07 r2:vehicle 占 C1-C9,std 电容 C1-C7 全灭)。先到先得登记+同前缀
    下一空号,保证请求名==落点名,注解引擎不再触发。"""
    catalog = _catalog()
    catalog["cap-100n"] = BlockRecord(
        block_id="cap-100n", name="cap", desc="x", lcsc="C123", pinout={"1": "A", "2": "B"}
    )
    plan = BlockPlan.model_validate(
        {
            "design_ir_id": "x",
            "source": "req.md",
            "blocks": [
                {
                    "block_id": "dc-terminal-wide-input",
                    "upstream_id": "block.vehicle_input_tps54360_5v",
                    "instance": "pwr1",
                    "ports_binding": {"VBAT_RAW": "12V", "5V": "5V", "GND": "GND"},
                },
                {
                    "block_id": "cap-100n",
                    "upstream_id": "",
                    "instance": "c1",
                    "pins_binding": {"1": "5V", "2": "GND"},
                },
            ],
        }
    )
    actions = compile_actions(plan, catalog)
    adapter = _DesigFakeAdapter("pass")
    lc = _loop(FakeChat("{}"), adapter, ir=_ir_loop(), tmp=str(tmp_path))
    lc._apply(actions, 1)
    # place 用了 C2 而非被占的 C1;autoconnect 引用 C2:1/C2:2
    places = [c for c in adapter.calls if c[:2] == ["sch", "place"]]
    assert places and places[0][places[0].index("--designator") + 1] == "C2"
    ac = [c for c in adapter.calls if c[:2] == ["sch", "autoconnect"]]
    assert sorted(c[c.index("--pin") + 1] for c in ac) == ["C2:1", "C2:2"]
    assert not any(c[c.index("--pin") + 1].startswith("C1:") for c in ac)


# ---- repack:两阶段布局(试放定框 → 离线装箱 → 逐页重放)----


class _RepackFakeAdapter(_ZoneFakeAdapter):
    """动态几何 fake:block-apply 按 --at/--doc 更新成员器件 box 模型,clusters
    按模型回真——试放与正式落图走同一套状态机。members 给每块的成员器件
    相对偏移 [(ox,oy,w,h)];fail_applies 指定哪些 apply 序号(从 0)直接失败。"""

    _DEFAULT_MEMBERS = [(0, 0, 300, 250), (350, 0, 300, 250)]  # 块并集 650×250

    def __init__(self, gate_verdict: str, instances: list[str], members: dict[str, list] | None = None,
                 fail_applies: set[int] | None = None, fail_place_adapter: bool = False,
                 fail_wires: bool = False, cascade: bool = False, blocked: bool = False) -> None:
        super().__init__(gate_verdict)
        self.instances = instances
        self.members = members or {}
        self.fail_applies = fail_applies or set()
        self.empty_applies: set[int] = set()  # 器件落地但 manifest stdout 空(unknown)
        self.silent_applies: set[int] = set()  # 一次性静默死:rc=0 空 stdout 零落物
        self.fail_place_adapter = fail_place_adapter
        self.fail_wires = fail_wires
        self.cascade = cascade
        self.blocked = blocked
        self.saw_clusters = False
        self.apply_seq = 0
        self.model: dict[str, dict[str, dict]] = {}  # page -> designator -> box
        self.placed: dict[str, list[str]] = {}  # instance -> 最近一次 manifest 位号
        self.pages: dict[str, str] = {}  # name -> uuid(_ensure_pages 建出来的页)
        self.page_seq = 0
        # 紧凑化数据面:每块一条内部网 X{seq}_N1(两成员脚同名 + 一对 netport),
        # sch list 按 page 合成 parts+pins+bbox+netports;wire/disconnect 记账
        self.nets: dict[str, dict[str, list]] = {}  # page -> net -> [desig,...]
        self.pins_by_page: dict[str, dict[str, list[dict]]] = {}  # page -> desig -> pins
        self.netports: dict[str, list[dict]] = {}  # page -> netport 组件列表
        self.wires: list[tuple[str, str, str]] = []  # (page, points-json, net) 页在
        # 线历史(含已随 sch clear 清掉的试放相位线——断言试放行为用,网表
        # 合成只读 self.wires 页在真值)
        self.wires_all: list[tuple[str, str, str]] = []
        self.disconnects: list[str] = []  # page:desig:net(--pin 形式)
        self.autoconnects: list[tuple[str, str, str]] = []  # 恢复路径(--pin --net)
        self.modifies: list[tuple[str, str, float]] = []  # page, designator, rotation
        self.connects: list[tuple] = []  # (page,pin,kind,net,dir,off) 确定性落桩
        # autoconnect 盲落标记夹具(默认关=不落标记,老测试零扰动):page →
        # [{net,"*"同配,dx,dy}] 命中时在脚+(dx,dy) 落 netport——真机 planner
        # 盲落必落标记且无几何检查(重跑#2 翼擦 12 根因),盲退护栏的质检对象
        self.ac_marks: dict[str, list[dict]] = {}
        self.active_page = "P1"
        # disconnect 拆 D:P 时连带清掉其它脚的网(真机:共享 netflag/连线被删,
        # 同网邻脚孤儿化——run-86f0ec3ab850 P2:C11 modify 回退拆 C10:2 的 GND)
        self.disconnect_tears: dict[str, list[str]] = {}  # "D:P" -> ["D2:P2",...]
        # >0 时 clusters 另出 body=box 左下内缩(模拟 netport 文字翼:volume 比
        # 本体大一圈)——量框口径分离(body 目检/ volume 装箱)的测试开关
        self.wing_inset = 0

    def run(self, args):
        self.calls.append(args)
        if args[:2] == ["sch", "block-apply"]:
            if self.apply_seq in self.silent_applies:
                # 静默死一次(run-88793706161c pwr_in:rc=0、stdout 空、零落物、
                # 不占 apply 序号)——同参重试即愈
                self.silent_applies.discard(self.apply_seq)
                return 0, "", ""
            if self.apply_seq in self.fail_applies:
                self.apply_seq += 1
                return 0, json.dumps({"ok": "failed-partial", "failure": "trial boom"}), ""
            inst = self.instances[self.apply_seq % len(self.instances)]
            seq = self.apply_seq
            self.apply_seq += 1
            page = args[args.index("--doc") + 1] if "--doc" in args else "P1"
            ax, ay = (float(v) for v in args[args.index("--at") + 1].split(","))
            des = []
            net = f"X{seq}_N1"  # 块编译器风格内部网名(紧凑化目标模式)
            self.nets.setdefault(page, {})[net] = []
            # --bind PORT=NET:边界网随块落图进入页网表(真机:端口脚并入网+
            # netflag 落纸)——_net_presence 的 planned 通道按 --bind 取网,
            # fake 不落网则 NET_MISSING e2e 全体假阳(保真补齐)
            for bi in range(len(args) - 1):
                if args[bi] == "--bind" and "=" in args[bi + 1]:
                    _bv = args[bi + 1].split("=", 1)
                    if _bv[1]:
                        self.nets.setdefault(page, {}).setdefault(_bv[1], [])
            for i, (ox, oy, w, h) in enumerate(self.members.get(inst, self._DEFAULT_MEMBERS)):
                d = f"{inst[:3].upper()}{seq}_{i + 1}"  # 带轮次:跨块不撞名
                self.model.setdefault(page, {})[d] = {
                    "minX": ax + ox, "minY": ay + oy, "maxX": ax + ox + w, "maxY": ay + oy + h}
                des.append({"designator": d})
                self.nets[page][net].append(d)
            # 两成员脚各带同名内部网 + 成对 netport(桩 20 横排,垂直错开让
            # Z 形直连可行:同轴同高会被"共线并线"约束判死)
            m0, m1 = self.members.get(inst, self._DEFAULT_MEMBERS)[:2]
            self.pins_by_page.setdefault(page, {})[self.nets[page][net][0]] = [
                {"pinNumber": "1", "x": ax + m0[0] + m0[2] - 10, "y": ay + m0[1] + 200, "net": net}]
            self.pins_by_page.setdefault(page, {})[self.nets[page][net][1]] = [
                {"pinNumber": "1", "x": ax + m1[0] + 10, "y": ay + m1[1] + 50, "net": net}]
            for k, (px, py) in enumerate((
                    (ax + m0[0] + m0[2] + 10, ay + m0[1] + 200),
                    (ax + m1[0] - 25, ay + m1[1] + 50))):
                self.netports.setdefault(page, []).append({
                    "primitiveId": f"np{seq}_{k}", "componentType": "netport",
                    "name": net, "net": net, "x": px, "y": py, "rotation": 0})
            self.placed[inst] = [x["designator"] for x in des]
            if self.cascade and seq == 0:
                # 级联夹具:N2 同行两脚(295/340,y=200),直线正压 N1 的标记
                # (310,200);四条 U 出列层(y±40/±80)各被一枚 N1 墙标记封死,
                # U 出行退化回行也被行上标记拦——第 1 轮 13 候选全灭;N1 转换
                # 后其标记/桩整体消失,第 2 轮直线解锁(不动点级联的判据)
                d0, d1 = self.nets[page][net][0], self.nets[page][net][1]
                net2 = f"X{seq}_N2"
                self.nets.setdefault(page, {})[net2] = [d0, d1]
                self.pins_by_page[page][d0].append(
                    {"pinNumber": "2", "x": ax + 295, "y": ay + 200, "net": net2})
                self.pins_by_page[page][d1].append(
                    {"pinNumber": "2", "x": ax + 340, "y": ay + 200, "net": net2})
                for wx, wy in ((310, 160), (310, 240), (310, 120), (310, 280)):
                    self.netports.setdefault(page, []).append({
                        "primitiveId": f"npw{seq}_{wx}_{wy}", "componentType": "netport",
                        "name": net, "net": net, "x": ax + wx, "y": ay + wy, "rotation": 0})
                for px, py in ((235, 160), (400, 160)):
                    self.netports.setdefault(page, []).append({
                        "primitiveId": f"np2_{seq}_{px}", "componentType": "netport",
                        "name": net2, "net": net2, "x": ax + px, "y": ay + py, "rotation": 0})
            if self.blocked and seq == 0:
                # 不可达夹具:N3 同行两脚(500/600),行心与他网(5V)脚同点、
                # 全部绕行行(y±40..±240)各被他网脚封死——他网脚是引脚不是
                # 标记,不动点轮次再多也移不走,恒 route-blocked。5V 脚全挂
                # 在 d0 上(脚坐标与成员 bbox 无关,fake 只按坐标算)。脚行
                # 取整到 10 网格:U 绕行行= snap(行±40..±240),墙脚必须恰好
                # 落在 snap 后的行上才压得住(同块脚容差 1.5<未对齐偏差);
                # v0.6.11 审计 P3 扩档 ±200/±240 后墙要封满全部绕行层
                # (旧墙只到 ±160,新档直接逃逸=假绿)。
                d0 = self.nets[page][net][0]
                d1 = self.nets[page][net][1]
                py = round((ay + 200) / 10.0) * 10.0
                net3 = f"X{seq}_N3"
                self.nets.setdefault(page, {})[net3] = [d0, d1]
                self.pins_by_page[page][d0].append(
                    {"pinNumber": "3", "x": ax + 500, "y": py, "net": net3})
                self.pins_by_page[page][d1].append(
                    {"pinNumber": "3", "x": ax + 600, "y": py, "net": net3})
                for i, dy in enumerate((0, -40, 40, -80, 80, -120, 120, -160, 160,
                                        -200, 200, -240, 240)):
                    self.pins_by_page[page][d0].append(
                        {"pinNumber": f"W{i}", "x": ax + 550, "y": py + dy, "net": "5V"})
                for px in (480, 620):
                    self.netports.setdefault(page, []).append({
                        "primitiveId": f"np3_{seq}_{px}", "componentType": "netport",
                        "name": net3, "net": net3, "x": ax + px, "y": py, "rotation": 0})
            if seq in self.empty_applies:
                # 真机形状(run-fd3f51113bdc dcin 块):器件已落但 stdout 空——
                # manifest {} → status=unknown,salvage 通道只读回真件
                return 0, "", ""
            return 0, json.dumps({"ok": "applied", "placed": des}), ""
        if args[:2] == ["sch", "clusters"]:
            self.saw_clusters = True  # 试放相位结束的边界(重放相位在此之后)
            page = args[args.index("--doc") + 1] if "--doc" in args else "P1"
            cs = []
            for d, b in self.model.get(page, {}).items():
                e = {"designator": d, "box": b}
                if self.wing_inset:
                    e["body"] = {"minX": b["minX"] + self.wing_inset,
                                 "minY": b["minY"] + self.wing_inset,
                                 "maxX": b["maxX"], "maxY": b["maxY"]}
                cs.append(e)
            return 0, json.dumps({"clusters": cs, "findings": [],
                                  "sheetUsable": {"minX": 12, "minY": 12, "maxX": 1158, "maxY": 813}}), ""
        if args[:3] == ["sch", "clear", "--doc"]:
            pg = args[args.index("--doc") + 1]
            self.model.pop(pg, None)
            self.nets.pop(pg, None)
            self.netports.pop(pg, None)
            self.pins_by_page.pop(pg, None)
            self.wires = [w for w in self.wires if w[0] != pg]
            return 0, "{}", ""
        if args[:2] == ["sch", "list"]:
            # 紧凑化数据面:parts(含 pins/bbox)+ netport 标记(--page 钉扎)
            pg = args[args.index("--page") + 1] if "--page" in args else self.active_page
            comps = []
            for d, b in self.model.get(pg, {}).items():
                c = {"primitiveId": f"pc_{d}", "componentType": "part",
                     "designator": d, "x": b["minX"], "y": b["minY"],
                     "bbox": dict(b),
                     "pins": self.pins_by_page.get(pg, {}).get(d, [])}
                comps.append(c)
            comps += [dict(f) for f in self.netports.get(pg, [])]
            return 0, json.dumps({"result": {"components": comps, "count": len(comps)}}), ""
        if args[:2] == ["sch", "wire"]:
            if self.fail_wires:
                return 1, "{}", "fake: wire boom"
            pts = args[args.index("--points") + 1]
            net = args[args.index("--net") + 1] if "--net" in args else ""
            self.wires.append((self.active_page, pts, net))
            self.wires_all.append((self.active_page, pts, net))
            # 真机语义:wire 端点落在脚上即并入该网(disconnect 清掉的 net 由
            # 此回填)——run-fd3f51113bdc 前的 fake 让 net 粘住,旋转/紧凑化
            # 的"拆了不重连"路径测试全绿是假(fake-reality 分歧,同 2026-08-25 #5)
            if net:
                try:
                    ends = [(float(x), float(y))
                            for x, y in (json.loads(pts) if pts.strip() else [])]
                except ValueError:
                    ends = []
                for pins in self.pins_by_page.get(self.active_page, {}).values():
                    for p in pins:
                        if p.get("x") is None:
                            continue
                        if any(abs(float(p["x"]) - ex) < 1 and abs(float(p["y"]) - ey) < 1
                               for ex, ey in ends):
                            p["net"] = net
            return 0, "{}", ""
        if args[:2] == ["sch", "disconnect"]:
            # --pin D:P:拆该脚的桩+netport(fake 语义:删该网在本页的全部 netport)
            if "--pin" in args:
                d, pnum = args[args.index("--pin") + 1].split(":", 1)
                pins = self.pins_by_page.get(self.active_page, {}).get(d) or []
                pin = next((p for p in pins if str(p.get("pinNumber")) == pnum), None)
                net = (pin or {}).get("net")
                if pin:
                    pin["net"] = ""  # 真机:disconnect 后脚 net 清空(按网找脚永远 None)
                if net:
                    # 真机:拆桩删该网标记,网零载体即从页网表消失(wire/autoconnect
                    # 可再建)——fake 网表键同步弹掉,NET_MISSING e2e 才测得出
                    # "恢复双失败=零载体断网"
                    self.nets.get(self.active_page, {}).pop(net, None)
                    self.netports[self.active_page] = [
                        f for f in self.netports.get(self.active_page, []) if f["net"] != net]
                    for ref in self.disconnect_tears.get(f"{d}:{pnum}", []):
                        td, tp = ref.split(":", 1)
                        for q in self.pins_by_page.get(self.active_page, {}).get(td) or []:
                            if str(q.get("pinNumber")) == tp:
                                q["net"] = ""  # 共享 netflag 被删,同网邻脚孤儿化
                self.disconnects.append(f"{self.active_page}:{d}:{net}")
            else:
                self.disconnects.append(args[-1])
            return 0, "{}", ""
        if args[:2] == ["lib", "search"]:
            return 0, json.dumps({"result": {"components": [{"libraryUuid": "L1", "uuid": "U1"}]}}), ""
        if args[:2] == ["sch", "place"]:
            page = args[args.index("--doc") + 1] if "--doc" in args else "P1"
            x = float(args[args.index("--x") + 1])
            y = float(args[args.index("--y") + 1])
            d = args[args.index("--designator") + 1]
            landed = d
            if any(d in boxes for pg, boxes in self.model.items() if pg != page):
                # 真机行为:EasyEDA 全工程位号去重,异页占名时落放被静默 +1 改号,
                # 而 place 回包的 designator 是 modify 的回显对象(=请求名)——
                # run-83ecf3862c01:ULN2U1(P1)→ P2 落成 ULN2U2,回包仍说 ULN2U1
                m = re.match(r"^(.*?)(\d+)$", d)
                if m:
                    all_d = {dd for boxes in self.model.values() for dd in boxes}
                    n = int(m.group(2)) + 1
                    while f"{m.group(1)}{n}" in all_d:
                        n += 1
                    landed = f"{m.group(1)}{n}"
            self.model.setdefault(page, {})[landed] = {
                "minX": x, "minY": y, "maxX": x + 80, "maxY": y + 40}
            return 0, json.dumps({"result": {"component": {"designator": d}}}), ""
        if args[:2] == ["sch", "modify"]:
            # 旋转:绕 bbox 中心刚性翻转 pin 坐标(180° 时 2 脚共线件即互换)
            if "--id" in args and "--rotation" in args:
                d = args[args.index("--id") + 1].removeprefix("pc_")
                rot = float(args[args.index("--rotation") + 1])
                b = self.model.get(self.active_page, {}).get(d)
                pins = self.pins_by_page.get(self.active_page, {}).get(d)
                if b and pins and abs(rot % 360) == 180:
                    cx, cy = (b["minX"] + b["maxX"]) / 2, (b["minY"] + b["maxY"]) / 2
                    for p in pins:
                        if p.get("x") is not None:
                            p["x"], p["y"] = 2 * cx - p["x"], 2 * cy - p["y"]
                self.modifies.append((self.active_page, d, rot))
            return 0, "{}", ""
        if args[:2] == ["sch", "connect"]:
            # 确定性落桩:锚点=脚坐标 ± offset(方向),gnd/power 落 netflag
            pin_ref = args[args.index("--pin") + 1]
            d, pnum = pin_ref.split(":", 1)
            kind = args[args.index("--kind") + 1]
            net = args[args.index("--net") + 1]
            direction = args[args.index("--direction") + 1]
            off = float(args[args.index("--offset") + 1])
            pins = self.pins_by_page.get(self.active_page, {}).get(d) or []
            pin = next((p for p in pins if str(p.get("pinNumber")) == pnum), None)
            if pin is None:
                return 1, "{}", "fake: no pin"
            pin["net"] = net  # 真机:connect 落桩后脚并入该网
            x, y = float(pin["x"]), float(pin["y"])
            if direction == "up":
                y += off
            elif direction == "down":
                y -= off
            elif direction == "left":
                x -= off
            elif direction == "right":
                x += off
            self.netports.setdefault(self.active_page, []).append({
                "primitiveId": f"cn_{pin_ref}",
                "componentType": "netflag" if kind in ("gnd", "power") else "netport",
                "net": net, "x": x, "y": y})
            self.connects.append((self.active_page, pin_ref, kind, net, direction, off))
            return 0, "{}", ""
        if args[:2] == ["sch", "autoconnect"]:
            if "--pin" in args and "--net" in args:
                # 真机:autoconnect 落桩+标记,脚并入该网(disconnect 清掉的 net
                # 由此回填——否则紧凑化的"wire 失败退 autoconnect"路径在 fake
                # 里永远缺网,断网测不出来)
                d, pnum = args[args.index("--pin") + 1].split(":", 1)
                net = args[args.index("--net") + 1]
                pin = next((p for p in self.pins_by_page
                            .get(self.active_page, {}).get(d, [])
                            if str(p.get("pinNumber")) == pnum), None)
                if pin:
                    pin["net"] = net
                    for spec in self.ac_marks.get(self.active_page, ()):
                        if spec.get("net") in ("*", net):
                            _pref = args[args.index("--pin") + 1]
                            self.netports.setdefault(self.active_page, []).append({
                                "primitiveId": f"ac_{_pref}_"
                                               f"{len(self.netports.get(self.active_page, []))}",
                                "componentType": "netport", "net": net,
                                "x": float(pin["x"]) + spec["dx"],
                                "y": float(pin["y"]) + spec["dy"]})
                            break
                # 真机:autoconnect 落桩即建网(place 通道块没有 pins_by_page
                # 模型,fake 里脚找不到也要让网进页网表,否则 NET_MISSING 假阳)
                self.nets.setdefault(self.active_page, {}).setdefault(net, [])
                self.autoconnects.append((self.active_page,
                                          args[args.index("--pin") + 1], net))
            return 0, "ok", ""
        if args[:2] == ["sch", "read"]:
            page = args[args.index("--page") + 1] if "--page" in args else "P1"
            comps = [{"designator": d, "pins": [{"number": "1", "name": "A"}, {"number": "2", "name": "B"}]}
                     for d in self.model.get(page, {})]
            # 真机契约:sch read 恒含 nets[](页网表)。合成口径=页网表键 ∪
            # 脚网 ∪ 标记网 ∪ 自画线网——disconnect 拆桩删标记、wire/autoconnect
            # 回填,四个通道的消长即 _net_presence 的 actual 演化(保真补齐)
            nets = set(self.nets.get(page, {}))
            for pins in self.pins_by_page.get(page, {}).values():
                nets.update(str(p.get("net") or "") for p in pins)
            nets.update(str(f.get("net") or "") for f in self.netports.get(page, []))
            nets.update(str(w[2]) for w in self.wires if w[0] == page)
            nets.discard("")
            return 0, json.dumps({"result": {
                "components": comps,
                "nets": [{"net": n, "name": n} for n in sorted(nets)]}}), ""
        if args[:2] == ["sch", "pages"]:
            # 真机形状:pages 带 parentSchematicUuid(建页要用);freeze-pack 的
            # _ensure_pages(["P2"]) 靠它真建 P2(真机 run-57223f61a0bd 教训:
            # 不建页直接 --doc P2,重放 4/4 全失败)
            pages = [{"name": "P1", "uuid": "u1", "parentSchematicUuid": "s1"}]
            pages += [{"name": n, "uuid": u, "parentSchematicUuid": "s1"}
                      for n, u in sorted(self.pages.items())]
            return 0, json.dumps({"result": {"pages": pages}}), ""
        if args[:2] == ["sch", "page-new"]:
            self.page_seq += 1
            return 0, json.dumps({"result": {"pageUuid": f"n{self.page_seq}"}}), ""
        if args[:2] == ["sch", "page-rename"]:
            self.pages[args[args.index("--name") + 1]] = args[args.index("--page") + 1]
            return 0, "{}", ""
        if args[:2] == ["sch", "open"]:
            uid = args[args.index("--page") + 1] if "--page" in args else ""
            rev = {u: n for n, u in {**{"P1": "u1"}, **self.pages}.items()}
            self.active_page = rev.get(uid, self.active_page)
            return 0, "{}", ""
        if args[:2] == ["debug", "exec"]:
            # 真机形状:JS 返回值嵌在 result.value 下(2026-08-25 debug exec 实测)
            return 0, json.dumps({"ok": True, "result": {"value": {"ok": True, "rects": ["r1"], "texts": ["t1"]}}}), ""
        return super().run(args)

    def run_json(self, args):
        # 环境失败仿真:试放相位的 sch place(--x 落虚空网格带 ≥1500;逐块
        # 落-量-清后 clusters 会穿插在 place 之前,不能再用它当相位边界)
        # 持续抛 AdapterError(--doc 活动页确认失败类)——真机 2026-08-25
        # req-07 曾因此 NameError 崩(except 引用了未导入名)。重放 place
        # 落纸内(<1500)恢复,验证只降级不崩、正式 place 照常。
        if args[:2] == ["sch", "place"] and self.fail_place_adapter \
                and "--x" in args and float(args[args.index("--x") + 1]) >= 1500:
            from edaloop.generate.adapter import AdapterError

            raise AdapterError('fake: --doc "P1": could not confirm active page')
        # repack 试放的 lib-search/sch place/sch read 走 run_json:分流到 run()
        # 的动态模型(否则落到父类 {"ok":"applied"} 兜底,uuid/器件全拿不到)
        if args[:2] in (["lib", "search"], ["sch", "place"], ["sch", "read"]):
            _rc, out, _err = self.run(args)
            return json.loads(out or "{}")
        return super().run_json(args)


def _audit_events(tmp: str) -> list[dict]:
    path = f"{tmp}/audit.jsonl"
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path, encoding="utf-8")]


def test_repack_orchestration(tmp_path) -> None:
    """两块 650×250:试放(P1)→ clusters 量框 → 装箱 → 正式 --at 被改写。
    2026-08-28 图签默认后:第 2 块换行位(30..680 × 480..730)撞图签角
    (486,645)→ 分两页——同页两块是旧基线(填满整带),新基线=2 页。"""
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _RepackFakeAdapter("pass", ["dcin1", "dcin2"])
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    flat = adapter.calls
    applies = [c for c in flat if c[:2] == ["sch", "block-apply"]]
    trial = [c for c in applies if "--doc" in c and c[c.index("--doc") + 1] == "P1" and
             any(cc[:2] == ["sch", "clusters"] for cc in flat[flat.index(c):] if cc[:2] == ["sch", "clear"])]
    # 1) 试放:两块都钉扎 P1,且 clusters 回读发生在试放后、清页前
    # (v0.6.11 gap 函数化:shelf 100 < 旧 200,第二块换行 y=195 恰清图签带
    #  ≤180 → 两块同页,不再分页;试放2 + 正式2 = 4)
    assert len([c for c in applies if "--doc" in c and c[c.index("--doc") + 1] == "P1"]) == 4
    reads = [c for c in flat if c[:2] == ["sch", "clusters"]]
    assert reads and reads[0][reads[0].index("--doc") + 1] == "P1"
    # P0-3:首条 clear 是试放画布清残(P1,发生在试放之前);量框期间不得
    # 有任何 clear(清了就把待量的墨迹毁了)
    first_clear = flat.index(next(c for c in flat if c[:3] == ["sch", "clear", "--doc"]))
    first_clusters = flat.index(reads[0])
    first_apply = flat.index(applies[0])
    assert first_clear < first_apply and flat[first_clear][-1] == "P1"
    assert not any(first_apply < i < first_clusters for i, c in enumerate(flat)
                   if c[:3] == ["sch", "clear", "--doc"])
    # 2) repack 审计链完整
    kinds = [e.get("kind") for e in _audit_events(str(tmp_path))]
    for ev in ("repack-trial", "repack-measure", "repack-pack"):
        assert ev in kinds
    # 3) 装箱改写生效:试放的 --at(流式初值)与正式的 --at(装箱位)不同
    trial_ats = {c[c.index("--at") + 1] for c in applies[:2]}
    final_ats = {c[c.index("--at") + 1] for c in applies[2:]}
    assert trial_ats and final_ats and trial_ats != final_ats
    # 4) v0.6.11 gap 函数化:shelf 100 让换行位 y=195 恰好清空图签带
    # (486..1140 × ≤180 与块 x 交叠但 y 无交),两块 650×250 合法同页
    pack_ev = next(e for e in _audit_events(str(tmp_path)) if e.get("kind") == "repack-pack")
    assert pack_ev["pages"] == 1 and pack_ev["oversize"] == []
    formal_docs = [c[c.index("--doc") + 1] for c in applies[2:]]
    assert formal_docs == ["P1", "P1"]
    # 5) 试放墨迹被清:P1 在逐页 clear 名单
    clears = [c for c in flat if c[:3] == ["sch", "clear", "--doc"]]
    assert "P1" in [c[c.index("--doc") + 1] for c in clears]


def test_repack_fallback_on_trial_failure(tmp_path) -> None:
    """试放全部块失败 → repack-fallback 审计 + 回退流式(仅正式 1 次 apply/块)。"""
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _RepackFakeAdapter("pass", ["dcin1", "dcin2"], fail_applies={0, 1})
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    applies = [c for c in adapter.calls if c[:2] == ["sch", "block-apply"]]
    assert len(applies) == 4  # 失败试放 2(全败 → 回退)+ 流式正式 2
    evs = _audit_events(str(tmp_path))
    fb = [e for e in evs if e.get("kind") == "repack-fallback"]
    assert fb and "trial-apply-all:dcin1" in fb[0]["reason"]
    # 回退后没有装箱审计
    assert "repack-pack" not in [e.get("kind") for e in evs]


def test_repack_single_trial_failure_degrades(tmp_path) -> None:
    """试放单块失败不弃全局:失败块退估算 cell,成功块照常用实测框。"""
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _RepackFakeAdapter("pass", ["dcin1", "dcin2"], fail_applies={0})
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    evs = _audit_events(str(tmp_path))
    assert "repack-fallback" not in [e.get("kind") for e in evs]
    trial_ev = next(e for e in evs if e.get("kind") == "repack-trial")
    assert any(f.startswith("dcin1:") for f in trial_ev.get("failed", []))
    # dcin1 退估算(est: 前缀),dcin2 实测(成员并集 650×250)
    meas = next(e for e in evs if e.get("kind") == "repack-measure")
    assert meas["cells"]["dcin1"].startswith("est:")
    assert meas["cells"]["dcin2"] == "650x250"


def test_repack_trial_unknown_salvages_measured(tmp_path) -> None:
    """manifest 空回救援(run-fd3f51113bdc dcin 块:stdout 空 → status=unknown →
    est cell 顶格上 oversize 页):rc=0 但 manifest {} 时块可能已落——只读回 P1
    真件(sch list 过滤 marker 伪位号)收救援审计,绝不重发 block-apply。
    v0.6.11 对抗评审(锚污染)后:实测锚不可信(manifest 空回=实际生效
    origin 不可知,上游钳位差可达 ~800)——救援块退 est+2*_EST_PAD 退化
    口径,审计带 anchor=est-degraded;尺寸换锚映射自洽,不污染 offsets。"""
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _RepackFakeAdapter("pass", ["dcin1", "dcin2"])
    adapter.empty_applies = {0}  # dcin1 落地但 stdout 空
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    evs = _audit_events(str(tmp_path))
    # 取证 + 救援审计都在
    unk = next(e for e in evs if e.get("kind") == "trial-manifest-unknown")
    assert unk["instance"] == "dcin1" and unk["keys"] == []
    sal = next(e for e in evs if e.get("kind") == "trial-salvage")
    assert sal["instance"] == "dcin1" and len(sal["members"]) == 2
    assert sal.get("anchor") == "est-degraded"
    # 救援块退 est 退化口径(锚不可信),dcin2 仍实测 650x250
    meas = next(e for e in evs if e.get("kind") == "repack-measure")
    assert meas["cells"]["dcin1"].startswith("est:")
    assert meas["cells"]["dcin2"] == "650x250"
    # 绝不重发:全 run 恰 4 次 block-apply(试放 2 + 正式重放 2),unknown
    # 救援是只读的,没有同参重放
    assert adapter.saw_clusters
    assert len([c for c in adapter.calls if c[:2] == ["sch", "block-apply"]]) == 4


def test_repack_trial_silent_death_degrades_no_retry(tmp_path) -> None:
    """静默死不重试(run-0019e7efcfec 定案:pwr_in/uart0 重试照旧空回,反把
    落-量-清的画布变更率翻倍,uart0 重试后 page-clear 两连 rc=1 整个试放崩退
    streaming):rc=0、stdout 空、P1 零真件 → est 兜底、trial_failed 记账,
    同参不重发(重放相位同块能正常落)。"""
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _RepackFakeAdapter("pass", ["dcin1", "dcin2"])
    adapter.silent_applies = {0}  # dcin1 静默死
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    evs = _audit_events(str(tmp_path))
    unk = [e for e in evs if e.get("kind") == "trial-manifest-unknown"]
    assert unk and unk[0]["instance"] == "dcin1"
    # 不重试:没有 trial-retry,也没有同参第二发
    assert not [e for e in evs if e.get("kind") in ("trial-retry", "trial-retry-error")]
    assert not [e for e in evs if e.get("kind") == "trial-salvage"]
    trial_ev = next(e for e in evs if e.get("kind") == "repack-trial")
    assert any(f.startswith("dcin1:unknown") for f in trial_ev.get("failed", []))
    meas = next(e for e in evs if e.get("kind") == "repack-measure")
    assert meas["cells"]["dcin1"].startswith("est:")
    assert meas["cells"]["dcin2"] == "650x250"
    # 全 run 恰 4 次 block-apply(试放 2 + 正式重放 2)——静默那次没有重发
    assert len([c for c in adapter.calls if c[:2] == ["sch", "block-apply"]]) == 4


def test_repack_replay_empty_stdout_retry_recovers(tmp_path, monkeypatch) -> None:
    """重放相位空回静默死补发一次(run-540fc4a72b9b:dc_in 试放+重放双双空回,
    P6 空页只有 est 框;run-86f0ec3ab850 同块试放死、重放活——重放位在带内、
    无逐块清页穿插,与试放相位(run-0019e7efcfec 定案不重试)不同,且空回=
    零落物、补发无双份风险)。keys 非空的 unknown(契约漂移)仍不试。"""
    monkeypatch.setenv("EDALOOP_LAYOUT_FREEZE", "pack")
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _RepackFakeAdapter("pass", ["dcin1", "dcin2"])
    adapter.silent_applies = {2}  # dcin1 的重放那发空回(试放 seq0/1,重放 seq2)
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "FREEZE"
    evs = _audit_events(str(tmp_path))
    ret = next(e for e in evs if e.get("kind") == "freeze-pack-replay-retry")
    assert ret["instance"] == "dcin1"
    rep = next(e for e in evs if e.get("kind") == "freeze-pack-replay")
    assert rep["failed"] == []  # 补发即愈,dcin1 不再进 failed
    # 全 run 5 次 block-apply(试放 2 + 重放 2 + 补发 1)
    assert len([c for c in adapter.calls if c[:2] == ["sch", "block-apply"]]) == 5
    # 补发落物:重放页有 dcin1 的件(2026-08-31 起装箱第 0 页=P1)
    assert adapter.model.get("P1") or adapter.model.get("P2")


def test_repack_place_channel_trial_measured(tmp_path) -> None:
    """place 通道块同试放:lib-search 预读 → P1 网格位 place → autoconnect 预演,
    量框拿到实测框(器件本体),不再恒用 _PLACE_INK 估算。"""
    plan = {
        "blocks": [
            {
                "block_id": "dc-terminal-wide-input",
                "upstream_id": "block.vehicle_input_tps54360_5v",
                "instance": "dcin1",
                "ports_binding": {"VBAT_RAW": "12V"},
            },
            {
                "block_id": "tactile-btn",
                "upstream_id": "",
                "instance": "btn1",
                "pins_binding": {"1": "3V3", "2": "GND"},
            },
        ],
        "nets": [],
        "uncovered": [],
        "confidence": 0.9,
        "provenance": [],
    }
    catalog = _catalog()
    catalog["tactile-btn"] = BlockRecord(
        block_id="tactile-btn", name="轻触开关", desc="x",
        category="peri", lcsc="C318884", pinout={"1": "A", "2": "B"},
    )

    def _cands(q=None):
        return _candidates() + [
            RetrievedBlock(
                block_id="tactile-btn", name="轻触开关", desc="x",
                category="peri", tags=[], parts=[], ports=[], provenance="",
                lcsc="C318884", pinout={"1": "A", "2": "B"},
                score=1.0, channels=["dense"],
            )
        ]

    chat = FakeChat(json.dumps(plan, ensure_ascii=False))
    adapter = _RepackFakeAdapter("pass", ["dcin1"])
    lc = LoopController(
        _ir_with_rails(("12V", 12.0), ("3V3", 3.3)), catalog,
        _cands, chat, adapter, AuditLog(str(tmp_path)),
    )
    result = lc.run()
    assert result.status == "PASS"
    evs = _audit_events(str(tmp_path))
    meas = next(e for e in evs if e.get("kind") == "repack-measure")
    assert meas["cells"]["btn1"] == "80x40"  # 实测(fake place 器件 80×40),非 est:
    # 试放 place(--x 落虚空网格带 ≥1500)钉扎 P1 且坐标来自试放网格(非流式
    # --x/--y);逐块落-量-清后 clusters 穿插在 place 之间,不再当相位边界
    trial_place = [c for c in adapter.calls
                   if c[:2] == ["sch", "place"]
                   and float(c[c.index("--x") + 1]) >= 1500]
    assert len(trial_place) == 1
    tp = trial_place[0]
    assert tp[tp.index("--doc") + 1] == "P1"
    # 试放 autoconnect 就地补齐在量测 clusters 之前(pin 名 BTN1:1/BTN1:2,P1)
    tp_i = adapter.calls.index(tp)
    reads = [i for i, c in enumerate(adapter.calls) if c[:2] == ["sch", "clusters"]]
    next_read = next(r for r in reads if r > tp_i)
    trial_ac = [c for i, c in enumerate(adapter.calls)
                if c[:2] == ["sch", "autoconnect"] and tp_i < i < next_read]
    assert len(trial_ac) == 2
    assert all(c[c.index("--doc") + 1] == "P1" for c in trial_ac)


def test_repack_place_adapter_error_degrades(tmp_path, monkeypatch) -> None:
    """place 试放吃 AdapterError(--doc 活动页确认失败)不崩不弃:单块退 est,
    repack 照常;正式重放照常 place。真机 req-07 曾在此 NameError 崩。"""
    plan = {
        "blocks": [
            {
                "block_id": "dc-terminal-wide-input",
                "upstream_id": "block.vehicle_input_tps54360_5v",
                "instance": "dcin1",
                "ports_binding": {"VBAT_RAW": "12V"},
            },
            {
                "block_id": "tactile-btn",
                "upstream_id": "",
                "instance": "btn1",
                "pins_binding": {"1": "3V3", "2": "GND"},
            },
        ],
        "nets": [],
        "uncovered": [],
        "confidence": 0.9,
        "provenance": [],
    }
    catalog = _catalog()
    catalog["tactile-btn"] = BlockRecord(
        block_id="tactile-btn", name="轻触开关", desc="x",
        category="peri", lcsc="C318884", pinout={"1": "A", "2": "B"},
    )

    def _cands(q=None):
        return _candidates() + [
            RetrievedBlock(
                block_id="tactile-btn", name="轻触开关", desc="x",
                category="peri", tags=[], parts=[], ports=[], provenance="",
                lcsc="C318884", pinout={"1": "A", "2": "B"},
                score=1.0, channels=["dense"],
            )
        ]

    chat = FakeChat(json.dumps(plan, ensure_ascii=False))
    # retry 的 8s 退避不进测试时钟
    monkeypatch.setattr("edaloop.loop.controller.time.sleep", lambda *_: None)
    # fail_place_adapter 只炸试放相位(clusters 回读前)的 sch place,重放恢复
    adapter = _RepackFakeAdapter("pass", ["dcin1"], fail_place_adapter=True)
    lc = LoopController(
        _ir_with_rails(("12V", 12.0), ("3V3", 3.3)), catalog,
        _cands, chat, adapter, AuditLog(str(tmp_path)),
    )
    result = lc.run()  # 不抛 NameError/不 fallback,轮次照常走完
    assert result.status == "PASS"
    evs = _audit_events(str(tmp_path))
    assert "repack-pack" in [e.get("kind") for e in evs]
    trial_ev = next(e for e in evs if e.get("kind") == "repack-trial")
    assert any(f.startswith("btn1:trial-adapter") for f in trial_ev.get("failed", []))
    meas = next(e for e in evs if e.get("kind") == "repack-measure")
    assert meas["cells"]["btn1"].startswith("est:")  # 退估算
    # 正式重放的 place 照常执行(fake 第二次恢复正常)
    reads = [i for i, c in enumerate(adapter.calls) if c[:2] == ["sch", "clusters"]]
    final_place = [c for i, c in enumerate(adapter.calls)
                   if c[:2] == ["sch", "place"] and i > reads[0]]
    assert len(final_place) == 1


def test_repack_freeze_draws_frames_and_stops(tmp_path, monkeypatch) -> None:
    """EDALOOP_LAYOUT_FREEZE=1:逐块落-量-清后在试放页(虚空网格位)画虚线框+
    左上坐标/长宽标注,轮次立即 FREEZE 收束——不装箱、不重放。"""
    monkeypatch.setenv("EDALOOP_LAYOUT_FREEZE", "1")
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _RepackFakeAdapter("pass", ["dcin1", "dcin2"])
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "FREEZE"
    evs = _audit_events(str(tmp_path))
    assert "repack-pack" not in [e.get("kind") for e in evs]  # 装箱/重放没跑
    fz = [e for e in evs if e.get("kind") == "trial-freeze"]
    assert fz and fz[0]["blocks"] == 2 and fz[0]["drawn"] is True
    # 画框前显式激活试放页 P1
    assert any(c[:2] == ["sch", "open"] and c[c.index("--page") + 1] == "u1"
               for c in adapter.calls)
    # debug exec 的 JS:每块一个虚线矩形原语 + 一个"实例 (左上x,左上y) 宽x高"标注
    code = next(c[c.index("--code") + 1] for c in adapter.calls if c[:2] == ["debug", "exec"])
    assert code.count("sch_PrimitiveRectangle.create") == 2
    assert code.count("sch_PrimitiveText.create") == 2
    assert code.count("650x250") == 2  # fake 实测框并集尺寸
    assert "dcin1 (" in code and "dcin2 (" in code  # 标注含左上坐标前缀
    # 逐块落-量-清:每次量测后清场给下一块腾地方,冻结页只留标注层(框+标签,
    # 无器件墨迹);清页只允许打 P1,且全部发生在画框(debug exec)之前
    draw_i = next(i for i, c in enumerate(adapter.calls) if c[:2] == ["debug", "exec"])
    clears = [(i, c) for i, c in enumerate(adapter.calls) if c[:3] == ["sch", "clear", "--doc"]]
    assert clears and all(c[-1] == "P1" and i < draw_i for i, c in clears)
    # 量测清场在试放 apply 之后穿插(每块一清),不是只清一次
    first_apply = next(i for i, c in enumerate(adapter.calls) if c[:2] == ["sch", "block-apply"])
    assert any(i > first_apply for i, _c in clears)


def test_repack_freeze_pack_replays_page1_to_p2(tmp_path, monkeypatch) -> None:
    """EDALOOP_LAYOUT_FREEZE=pack:packer.pack 全量装箱,第 p 页块真实落 P{p+1}
    (锚=装箱位-试放偏移;2026-08-31 用户定案从 P1 起,与生产路径同式,P1 不再
    留试放标注层),逐页画框+标注,轮次 FREEZE。
    2026-08-31 gap 基线:shelf 62 让两块 650×250 双行总高 562 ≤ 带高 765
    且第二行 y=233 恰清图签带 → 同页。"""
    monkeypatch.setenv("EDALOOP_LAYOUT_FREEZE", "pack")
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _RepackFakeAdapter("pass", ["dcin1", "dcin2"])
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "FREEZE"
    evs = _audit_events(str(tmp_path))
    # 装箱审计存在(2 块同页);装箱第 0 页=P1(P1 即交付首页)
    pack_ev = next(e for e in evs if e.get("kind") == "repack-pack")
    assert pack_ev["pages"] == 1 and pack_ev["oversize"] == []
    assert {v[0] for v in pack_ev["placements"].values()} == {"P1"}
    replay_ev = next(e for e in evs if e.get("kind") == "freeze-pack-replay")
    assert replay_ev["pages"] == {"P1": ["dcin1", "dcin2"]} and replay_ev["failed"] == []
    # 试放 2 次(P1 虚空网格)+ 重放每块各 1 次(P1),重放 --at = 装箱位
    # (fake 偏移 0)
    applies = [c for c in adapter.calls if c[:2] == ["sch", "block-apply"]]
    p2 = [c for c in applies if c[c.index("--doc") + 1] == "P1"
          and float(c[c.index("--at") + 1].split(",")[0]) < 1500]
    assert len(p2) == 2
    # P1 已存在:本轮无 page-new(旧 P{p+2} 时代要建 P2;真机 run-57223f61a0bd
    # 教训的建页防线只对新增页生效)
    assert not [c for c in adapter.calls if c[:2] == ["sch", "page-new"]]
    for c in p2:
        at = c[c.index("--at") + 1]
        px, py = (float(v) for v in at.split(","))
        assert 30 <= px and px + 650 <= 1140 and 30 <= py and py + 250 <= 795  # 落 A4 装箱带内
    placements = pack_ev["placements"]
    for inst, c in (("dcin1", p2[0]), ("dcin2", p2[1])):
        _pg, x, y = placements[inst]
        assert c[c.index("--at") + 1] == f"{x},{y}"
    # 画框一次:P1 两块(KEEP_P1 对交付页无效——P1 是生产页,试放标注层
    # 永不画;无 BAND 参考框——旧目检实验已撤)
    codes = [c[c.index("--code") + 1] for c in adapter.calls if c[:2] == ["debug", "exec"]]
    assert len(codes) == 1
    assert codes[0].count("sch_PrimitiveRectangle.create") == 2  # P1:2 块框
    assert "BAND" not in codes[0]
    # 冻结收束:clear 只打 P1(逐块量测清场 + 重放前验证式清页,先于画框);
    # P1 是交付页 → 画框后不再有 P1 清场(旧「收尾清标注层」已随 P{p+2}
    # 映射一并废除,清=毁交付物)
    draw2_i = [i for i, c in enumerate(adapter.calls) if c[:2] == ["debug", "exec"]][-1]
    clears = [(i, c) for i, c in enumerate(adapter.calls) if c[:3] == ["sch", "clear", "--doc"]]
    assert all(c[c.index("--doc") + 1] == "P1" for _i, c in clears)
    assert any(c[c.index("--doc") + 1] == "P1" and i < draw2_i for i, c in clears)
    assert not any(c[c.index("--doc") + 1] == "P1" and i > draw2_i for i, c in clears)


def test_repack_freeze_frames_use_body_union_cells_stay_volume(tmp_path, monkeypatch) -> None:
    """量框口径分离(run-47827896dd04:跨块 netport 文字翼实测拖 300+ mil,把
    目检框撑成跨排互叠,13 对重叠全是翼越带、本体无叠):目检框/尺寸标注/
    repack-measure 用 body 本体并集;生产装箱 cell 保持 volume(含翼)——块
    模板的桩+文字在正式页照样落墨,cell 收小会破坏"翼不越 cell + 200 gap
    归属安全距"的已验证前提。"""
    monkeypatch.setenv("EDALOOP_LAYOUT_FREEZE", "pack")
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _RepackFakeAdapter("pass", ["dcin1", "dcin2"])
    adapter.wing_inset = 100  # 每成员 volume=box、body=box 左下各内缩 100
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "FREEZE"
    evs = _audit_events(str(tmp_path))
    # 全量计划入审计(2026-08-31):LLM 配额断供后"不调 LLM 按原计划重跑"
    # 依赖它——此前只记块名,计划无处可取
    rplan = next(e for e in evs if e.get("kind") == "round-plan")
    assert {b["instance"] for b in rplan["plan"]["blocks"]} == {"dcin1", "dcin2"}
    assert rplan["plan"]["blocks"][0]["ports_binding"] == {"VBAT_RAW": "12V"}
    # 冻结轮也有量框审计(freeze 分支之前发),尺寸是 body 口径 550x150
    meas = next(e for e in evs if e.get("kind") == "repack-measure")
    assert meas["cells"]["dcin1"] == "550x150" and meas["cells"]["dcin2"] == "550x150"
    # 试放锚点全部落在 A4 纸外(带 x1=1500 > 纸右缘 1170):分步布局第一步
    # "纸内一个不放"(旧带 x1=30 起点在纸内,13/37 块排进纸面)。P1 现在同时
    # 是重放交付页(x=30 起步),按 x≥1500 过滤出试放发
    trial_at = [float(c[c.index("--at") + 1].split(",")[0])
                for c in adapter.calls if c[:2] == ["sch", "block-apply"]
                and c[c.index("--doc") + 1] == "P1"
                and float(c[c.index("--at") + 1].split(",")[0]) >= 1500]
    assert trial_at and len(trial_at) == 2
    # P2 目检框标注是 volume 尺寸(v0.6.11 审计 P1 反转旧 body 决策:框必须
    # 框住全部墨迹——body 框只并本体,netport 文字/桩线全在框外,正是
    # 「框不住墨迹→模块重叠」目检的根因);KEEP_P1=0 → 无 P1 试放层,
    # v0.6.11 gap 后两块同页单次画框
    codes = [c[c.index("--code") + 1] for c in adapter.calls if c[:2] == ["debug", "exec"]]
    assert len(codes) == 1
    assert codes[0].count("650x250") == 2  # P2 框 2 块,volume 口径
    assert all("550x150" not in code for code in codes)
    # 装箱仍按 volume cell:一行放不下两块(650×2+60>带宽 1110)换行;
    # 锚 y=795-250=545 即 cell 高 250 的体积锚(body 收小会变成 795-150=645),
    # 次行 y=545-62(shelf gap 25%)-250=233 恰清图签带 → 同页
    pack_ev = next(e for e in evs if e.get("kind") == "repack-pack")
    (pa, x1, y1), (pb, x2, y2) = (pack_ev["placements"][n] for n in ("dcin1", "dcin2"))
    assert pa == pb
    assert (x1, y1) == (30.0, 545.0) and (x2, y2) == (30.0, 233.0)


def test_repack_freeze_pack_place_designator_renamed(tmp_path, monkeypatch) -> None:
    """freeze-pack 的 place 重放撞名:EasyEDA 静默 +1(BTN1→BTN2)而回包回显
    请求名——控制器按 --x/--y 锚点回读认领真实落名,autoconnect 换名重放,
    P2 框不退 est(run-83ecf3862c01:16 脚全空 + 550x500 est 框越 BAND)。"""
    monkeypatch.setenv("EDALOOP_LAYOUT_FREEZE", "pack")
    plan = {
        "blocks": [
            {
                "block_id": "dc-terminal-wide-input",
                "upstream_id": "block.vehicle_input_tps54360_5v",
                "instance": "dcin1",
                "ports_binding": {"VBAT_RAW": "12V"},
            },
            {
                "block_id": "tactile-btn",
                "upstream_id": "",
                "instance": "btn1",
                "pins_binding": {"1": "3V3", "2": "GND"},
            },
        ],
        "nets": [],
        "uncovered": [],
        "confidence": 0.9,
        "provenance": [],
    }
    catalog = _catalog()
    catalog["tactile-btn"] = BlockRecord(
        block_id="tactile-btn", name="轻触开关", desc="x",
        category="peri", lcsc="C318884", pinout={"1": "A", "2": "B"},
    )

    def _cands(q=None):
        return _candidates() + [
            RetrievedBlock(
                block_id="tactile-btn", name="轻触开关", desc="x",
                category="peri", tags=[], parts=[], ports=[], provenance="",
                lcsc="C318884", pinout={"1": "A", "2": "B"},
                score=1.0, channels=["dense"],
            )
        ]

    chat = FakeChat(json.dumps(plan, ensure_ascii=False))
    adapter = _RepackFakeAdapter("pass", ["dcin1"])
    lc = LoopController(
        _ir_with_rails(("12V", 12.0), ("3V3", 3.3)), catalog,
        _cands, chat, adapter, AuditLog(str(tmp_path)),
    )
    result = lc.run()
    assert result.status == "FREEZE"
    evs = _audit_events(str(tmp_path))
    # 撞名防线成立:试放量测累计过 BTN1(P1 试放墨迹已随逐块清场消失,但累计名
    # 仍触发主动避撞),P1 重放落 BTN2(2026-08-31 D1:交付页与试放层同为 P1,
    # 试放锚 x≥1500 在纸外虚空,重放锚在带内)
    assert "BTN2" in adapter.model.get("P1", {}) and "BTN1" not in adapter.model.get("P1", {})
    # 主动避撞:place 前就把 --designator 改成自由名并审计
    ren = next(e for e in evs if e.get("kind") == "designator-rename")
    assert (ren["want"], ren["actual"]) == ("BTN1", "BTN2")
    # 重放的 autoconnect 用换名后的 BTN2(旧代码找 BTN1 全灭且无审计)。
    # D1 后试放与重放同页,按调用序切:首发重放 block-apply(纸内 x<1500)
    # 之后的 autoconnect 才是重放段(试放段用的还是 want 名 BTN1)
    _replay_i = next(i for i, c in enumerate(adapter.calls)
                     if c[:2] == ["sch", "block-apply"] and "--doc" in c
                     and c[c.index("--doc") + 1] == "P1"
                     and float(c[c.index("--at") + 1].split(",")[0]) < 1500)
    p1_ac = [c for c in adapter.calls[_replay_i:]
             if c[:2] == ["sch", "autoconnect"] and "--doc" in c
             and c[c.index("--doc") + 1] == "P1"]
    assert {c[c.index("--pin") + 1] for c in p1_ac} == {"BTN2:1", "BTN2:2"}
    assert "freeze-pack-autoconnect" not in [e.get("kind") for e in evs]
    # 交付页框:btn1 回查命中(BTN2 在 clusters),不再退 est(试放层墨迹
    # 随逐块清场消失,单次画框)
    codes = [c[c.index("--code") + 1] for c in adapter.calls if c[:2] == ["debug", "exec"]]
    assert len(codes) == 1
    assert "est btn1" not in codes[0] and "btn1" in codes[0]


def test_repack_freeze_pack_duplicate_template_designator(tmp_path, monkeypatch) -> None:
    """同名模板位号坍缩(run-0cdc61dd3eea:uln2003_ch1/ch2 的位号经 8 字符截断
    都要 ULN2003C,双双主动避撞改名 ULN2003C1/2;旧代码 renamed_r 按想要名
    键控,ch2 覆盖 ch1,autoconnect 两块全解析到 ULN2003C2——ULN2003C1 全
    16 脚空网,审计却报"ULN2003C2:1-4/13-16 失败"假象)。修=按块实例键控。"""
    monkeypatch.setenv("EDALOOP_LAYOUT_FREEZE", "pack")
    plan = {
        "blocks": [
            {
                "block_id": "dc-terminal-wide-input",
                "upstream_id": "block.vehicle_input_tps54360_5v",
                "instance": "dcin1",
                "ports_binding": {"VBAT_RAW": "12V"},
            },
            {
                "block_id": "uln-drv", "upstream_id": "", "instance": "uln2003_ch1",
                "pins_binding": {"1": "STEP1_A", "2": "GND"},
            },
            {
                "block_id": "uln-drv", "upstream_id": "", "instance": "uln2003_ch2",
                "pins_binding": {"1": "STEP2_A", "2": "GND"},
            },
        ],
        "nets": [],
        "uncovered": [],
        "confidence": 0.9,
        "provenance": [],
    }
    catalog = _catalog()
    catalog["uln-drv"] = BlockRecord(
        block_id="uln-drv", name="ULN2003 通道", desc="x",
        category="peri", lcsc="C7512", pinout={"1": "IN", "2": "OUT"},
    )

    def _cands(q=None):
        return _candidates() + [
            RetrievedBlock(
                block_id="uln-drv", name="ULN2003 通道", desc="x",
                category="peri", tags=[], parts=[], ports=[], provenance="",
                lcsc="C7512", pinout={"1": "IN", "2": "OUT"},
                score=1.0, channels=["dense"],
            )
        ]

    chat = FakeChat(json.dumps(plan, ensure_ascii=False))
    adapter = _RepackFakeAdapter("pass", ["dcin1"])
    lc = LoopController(
        _ir_with_rails(("12V", 12.0), ("3V3", 3.3)), catalog,
        _cands, chat, adapter, AuditLog(str(tmp_path)),
    )
    result = lc.run()
    assert result.status == "FREEZE"
    evs = _audit_events(str(tmp_path))
    # 双双避撞:两块各自改到自己的自由名(ULN2003C → C1/C2)
    ren = {(e["instance"], e["want"], e["actual"]) for e in evs
           if e.get("kind") == "designator-rename"}
    assert ren == {("uln2003_ch1", "ULN2003C", "ULN2003C1"),
                   ("uln2003_ch2", "ULN2003C", "ULN2003C2")}
    # autoconnect 各归各件:ch1 的网落在 ULN2003C1、ch2 的落在 ULN2003C2
    # (旧代码两块全打 ULN2003C2,C1 零连线)。2026-08-31 D1:试放与重放同在
    # P1,按调用序切重放段(试放段两块都还引用截断名 ULN2003C)
    _replay_i = next(i for i, c in enumerate(adapter.calls)
                     if c[:2] == ["sch", "block-apply"] and "--doc" in c
                     and c[c.index("--doc") + 1] == "P1"
                     and float(c[c.index("--at") + 1].split(",")[0]) < 1500)
    replay_ac = {(c[c.index("--pin") + 1], c[c.index("--net") + 1])
                 for c in adapter.calls[_replay_i:]
                 if c[:2] == ["sch", "autoconnect"] and "--doc" in c
                 and c[c.index("--doc") + 1] == "P1"}
    assert replay_ac == {("ULN2003C1:1", "STEP1_A"), ("ULN2003C1:2", "GND"),
                         ("ULN2003C2:1", "STEP2_A"), ("ULN2003C2:2", "GND")}
    assert "freeze-pack-autoconnect" not in [e.get("kind") for e in evs]


def test_freeze_pack_rotate_outward_pins(tmp_path) -> None:
    """对脚旋转(run-fc264cf3ac76 目检:LED12_N2 走 U 形 200,直连只需 80):
    2 脚共线件 LED12 的 N2 桩背离伙伴 R21 →180° 刚性旋转两脚互换,modify 后
    重列验证互换才经 sch connect(确定性方向+桩长)重连;桩朝向伙伴的 R21 不动。"""
    chat = FakeChat("{}")
    adapter = _RepackFakeAdapter("pass", [])
    adapter.model["P1"] = {
        "LED12": {"minX": 1080, "minY": 750, "maxX": 1120, "maxY": 770},
        "R21": {"minX": 940, "minY": 750, "maxX": 1000, "maxY": 770},
    }
    adapter.pins_by_page["P1"] = {
        "LED12": [{"pinNumber": "1", "x": 1120, "y": 760, "net": "LED12_N2"},
                  {"pinNumber": "2", "x": 1080, "y": 760, "net": "GND"}],
        "R21": [{"pinNumber": "1", "x": 960, "y": 760, "net": "LED_FAULT"},
                {"pinNumber": "2", "x": 1000, "y": 760, "net": "LED12_N2"}],
    }
    adapter.netports["P1"] = [
        {"primitiveId": "m1", "componentType": "netport", "net": "LED12_N2", "x": 1140, "y": 760},
        {"primitiveId": "m2", "componentType": "netport", "net": "LED12_N2", "x": 1020, "y": 760},
        {"primitiveId": "m3", "componentType": "netflag", "net": "GND", "x": 1060, "y": 760},
        {"primitiveId": "m4", "componentType": "netport", "net": "LED_FAULT", "x": 940, "y": 760},
    ]
    lc = LoopController(_ir_with_rails(("12V", 12.0)), _catalog(), _candidates,
                        chat, adapter, AuditLog(str(tmp_path)))
    lc._rotate_outward_pins("P1", 1)
    evs = _audit_events(str(tmp_path))
    ev = next(e for e in evs if e.get("kind") == "freeze-pack-rotate")
    assert [r["part"] for r in ev["rotated"]] == ["LED12"] and ev["failed"] == []
    assert ev["rotated"][0]["net"] == "LED12_N2"
    assert ev["rotated"][0]["stubs"] == [["left", 30], ["left", 30]]
    # 旋转调用:--id 指 primitiveId,--rotation 180;R21 未被转
    mods = [c for c in adapter.calls if c[:2] == ["sch", "modify"]]
    assert len(mods) == 1
    assert mods[0][mods[0].index("--id") + 1] == "pc_LED12"
    assert mods[0][mods[0].index("--rotation") + 1] == "180"
    # 两脚确已互换:pin1(N2)落在旧 pin2 位(1080,760)
    p1 = next(p for p in adapter.pins_by_page["P1"]["LED12"] if p["pinNumber"] == "1")
    assert (p1["x"], p1["y"]) == (1080, 760)
    # 两脚都先拆后连;确定性落桩按新坐标:两脚都选 left/30(离带边最远且
    # 不穿自件脚),不走 planner 彩票
    assert {c[c.index("--pin") + 1] for c in adapter.calls
            if c[:2] == ["sch", "disconnect"]} == {"LED12:1", "LED12:2"}
    assert [(c[1], c[4], c[5]) for c in adapter.connects if c[0] == "P1"] == [
        ("LED12:1", "left", 30.0), ("LED12:2", "left", 30.0)]
    marks = {f["net"]: f for f in adapter.netports["P1"]
             if str(f.get("primitiveId", "")).startswith("cn_")}
    assert (marks["LED12_N2"]["x"], marks["LED12_N2"]["y"]) == (1050, 760)
    assert (marks["GND"]["x"], marks["GND"]["y"]) == (1090, 760)
    assert all(30 <= f["x"] <= 1140 and 30 <= f["y"] <= 795 for f in marks.values())


def test_freeze_pack_rotate_perpendicular_stub(tmp_path) -> None:
    """垂直桩旋转(run-fd3f51113bdc P4:LED4/6/7 不旋、直连线穿体绕行):桩方向
    与伙伴方向垂直(dot=0)是模板常态——旧触发只认"背离 dot<-0.3",垂直桩
    永不触发;新触发只挡"桩已朝向伙伴(dot>0.3)"。本例桩向下、伙伴在右、
    对脚在伙伴侧 → LED4 旋转;拆线清 net 后按脚号验证互换+重连回填 net。"""
    chat = FakeChat("{}")
    adapter = _RepackFakeAdapter("pass", [])
    adapter.model["P1"] = {
        "LED4": {"minX": 390, "minY": 690, "maxX": 450, "maxY": 710},
        "R13": {"minX": 540, "minY": 690, "maxX": 580, "maxY": 710},
    }
    adapter.pins_by_page["P1"] = {
        "LED4": [{"pinNumber": "1", "x": 400, "y": 700, "net": "LED4_N1"},
                 {"pinNumber": "2", "x": 440, "y": 700, "net": "GND"}],
        "R13": [{"pinNumber": "1", "x": 560, "y": 700, "net": "LED4_N1"},
                {"pinNumber": "2", "x": 580, "y": 700, "net": "SRC_N1"}],
    }
    adapter.netports["P1"] = [
        # LED4:1 的桩在脚正下方(垂直于伙伴方向);R13:1 的桩朝向 LED4(不动)
        {"primitiveId": "m1", "componentType": "netport", "net": "LED4_N1", "x": 400, "y": 670},
        {"primitiveId": "m2", "componentType": "netport", "net": "LED4_N1", "x": 540, "y": 700},
    ]
    lc = LoopController(_ir_with_rails(("12V", 12.0)), _catalog(), _candidates,
                        chat, adapter, AuditLog(str(tmp_path)))
    lc._rotate_outward_pins("P1", 1)
    evs = _audit_events(str(tmp_path))
    ev = next(e for e in evs if e.get("kind") == "freeze-pack-rotate")
    assert [r["part"] for r in ev["rotated"]] == ["LED4"] and ev["failed"] == []
    # 只有 LED4 被 180° 旋转;R13(桩朝向伙伴)不动
    mods = [c for c in adapter.calls if c[:2] == ["sch", "modify"] and "--rotation" in c]
    assert len(mods) == 1 and mods[0][mods[0].index("--id") + 1] == "pc_LED4"
    # 两脚互换:pin1(LED4_N1)落到旧 pin2 位
    p1 = next(p for p in adapter.pins_by_page["P1"]["LED4"] if p["pinNumber"] == "1")
    assert (p1["x"], p1["y"]) == (440, 700)
    # 重连回填:disconnect 清掉的 net 由 connect 写回(裸奔根因的回归锚)
    p2 = next(p for p in adapter.pins_by_page["P1"]["LED4"] if p["pinNumber"] == "2")
    assert p1["net"] == "LED4_N1" and p2["net"] == "GND"


def test_repack_rail_lone_pull_shrinks_block(tmp_path) -> None:
    """rail 孤件拉近(run-fd3f51113bdc P7:ldo 块 4 件全 rail——无内部网名,直连/
    净距两通道都不触发,电容停模板位离稳压器 300-480,块摊成 oversize 独占页):
    全脚皆 rail/空网的小件离锚件(脚多者)最近脚距 > 150 → 拉到锚脚旁。门:
    近距件(C7 110)不拉、带非 rail 网的件(R9)归净距/直连通道管。"""
    chat = FakeChat("{}")
    adapter = _PullFakeAdapter("pass", [])
    adapter.model["P1"] = {
        "U4": {"minX": 1000, "minY": 500, "maxX": 1100, "maxY": 600},
        "C5": {"minX": 1500, "minY": 500, "maxX": 1540, "maxY": 540},
        "C6": {"minX": 1000, "minY": 200, "maxX": 1040, "maxY": 240},
        "C7": {"minX": 1120, "minY": 620, "maxX": 1160, "maxY": 660},
        "R9": {"minX": 1200, "minY": 280, "maxX": 1240, "maxY": 320},
    }
    adapter.pins_by_page["P1"] = {
        "U4": [{"pinNumber": "1", "x": 1000, "y": 530, "net": "3V3"},
               {"pinNumber": "2", "x": 1100, "y": 530, "net": "5V"},
               {"pinNumber": "3", "x": 1050, "y": 600, "net": "GND"}],
        "C5": [{"pinNumber": "1", "x": 1500, "y": 530, "net": "5V"},
               {"pinNumber": "2", "x": 1540, "y": 530, "net": "GND"}],
        "C6": [{"pinNumber": "1", "x": 1000, "y": 220, "net": "3V3"},
               {"pinNumber": "2", "x": 1040, "y": 220, "net": "GND"}],
        "C7": [{"pinNumber": "1", "x": 1120, "y": 640, "net": "GND"},
               {"pinNumber": "2", "x": 1160, "y": 640, "net": "GND"}],
        "R9": [{"pinNumber": "1", "x": 1200, "y": 300, "net": "LDO4_N1"},
               {"pinNumber": "2", "x": 1240, "y": 300, "net": "LDO4_N1"}],
    }
    lc = LoopController(_ir_with_rails(("12V", 12.0), ("3V3", 3.3)), _catalog(),
                        _candidates, chat, adapter, AuditLog(str(tmp_path)))
    n = lc._pull_long_pairs("P1", 1, {"ldo4": ["U4", "C5", "C6", "C7", "R9"]})
    assert n == 2
    evs = _audit_events(str(tmp_path))
    ev = next(e for e in evs if e.get("kind") == "pull-close")
    assert sorted(p["moved"] for p in ev["pulls"]) == ["C5", "C6"]
    assert all(p["net"] == "(rail)" and p["via"] == "group-move" for p in ev["pulls"])
    assert not [e for e in evs if e.get("kind") == "pull-close-fail"]
    # 拉后紧邻锚件(块体积坍缩,不再 oversize);近距件/非 rail 件原地不动
    u4 = adapter.model["P1"]["U4"]
    for moved in ("C5", "C6"):
        b = adapter.model["P1"][moved]
        gap = max(u4["minX"] - b["maxX"], b["minX"] - u4["maxX"], 0) + \
            max(u4["minY"] - b["maxY"], b["minY"] - u4["maxY"], 0)
        assert gap <= 250, moved
    assert adapter.model["P1"]["C7"]["minX"] == 1120
    assert adapter.model["P1"]["R9"]["minX"] == 1200


def test_move_touch_conflict_variants(tmp_path) -> None:
    """移动触点预检四形态(run-b2c1990f44a4 定性:拉移拖着桩+标记平移,终位
    落上他网点/段即并网粘死——pin.net 接触瞬间翻写不回滚,GND isGlobal 一点
    并轨全工程塌):异网共端点=冲突/同名网=无害/动点落他网段(T结)=冲突/
    他网点落动段=冲突。"""
    chat = FakeChat("{}")
    adapter = _RepackFakeAdapter("pass", [])
    lc = LoopController(_ir_with_rails(("12V", 12.0)), _catalog(),
                        _candidates, chat, adapter, AuditLog(str(tmp_path)))
    comps = [
        {"componentType": "part", "designator": "UA",
         "pins": [{"pinNumber": "1", "x": 395, "y": 530, "net": "A_N1"}]},
        {"componentType": "part", "designator": "UB",
         "pins": [{"pinNumber": "1", "x": 555, "y": 530, "net": "B_N2"}]},
        {"componentType": "netport", "net": "A_N1", "x": 365, "y": 530},
        {"componentType": "netport", "net": "B_N2", "x": 585, "y": 530},
    ]
    emap = lc._page_electrical_map(comps)
    # 标记归属:同网锚距脚 ≤120 挂到件;两件各成一段桩线
    assert {r[3] for r in emap["marks"]} == {"UA", "UB"}
    assert len(emap["segs"]) == 2
    # ① 异网共端点:UA 平移 (160,0) → 脚落 (555,530) ≡ UB 脚
    assert lc._move_touch_conflict("UA", 160, 0, emap)
    # ② 同名网接触无害(netport 语义本身):全改 GND 后同样平移不冲突
    emap_gnd = lc._page_electrical_map([
        {"componentType": "part", "designator": "UA",
         "pins": [{"pinNumber": "1", "x": 395, "y": 530, "net": "GND"}]},
        {"componentType": "part", "designator": "UB",
         "pins": [{"pinNumber": "1", "x": 555, "y": 530, "net": "GND"}]},
        {"componentType": "netflag", "net": "GND", "x": 365, "y": 530},
        {"componentType": "netflag", "net": "GND", "x": 585, "y": 530},
    ])
    assert not lc._move_touch_conflict("UA", 160, 0, emap_gnd)
    # ③ 动点落他网段:UA 平移 (175,0) → 脚落 (570,530) 在 UB 桩段上(T 结)
    assert lc._move_touch_conflict("UA", 175, 0, emap)
    # ④ 他网点落动段:UB 脚挪 (540,530)(与 UA 终位脚相距 15 不触),UA 平移
    #    (160,0) → 动段 (525,530)-(555,530) 覆盖 (540,530)
    emap4 = lc._page_electrical_map([
        {"componentType": "part", "designator": "UA",
         "pins": [{"pinNumber": "1", "x": 395, "y": 530, "net": "A_N1"}]},
        {"componentType": "part", "designator": "UB",
         "pins": [{"pinNumber": "1", "x": 540, "y": 530, "net": "B_N2"}]},
        {"componentType": "netport", "net": "A_N1", "x": 365, "y": 530},
        {"componentType": "netport", "net": "B_N2", "x": 585, "y": 530},
    ])
    assert lc._move_touch_conflict("UA", 160, 0, emap4)


def _pull_guard_scene(adapter: _PullFakeAdapter, with_rf: bool) -> None:
    """拉近守卫场景:U4 锚(顶中脚 A),C5 全 rail 远件拉到 A 旁;B1/B2 封
    上下(含对角远角——对角候选是未归一化向量,×190 双轴各移 190)、RB 封右,
    唯一活空位族=(-1,0)×{90,140,190}。RF=异网细脚件钉在 90 档标记落点
    (880,480)——箱子细到不挡 90 档空位,只有电气门看得见它;但足以箱挡
    190 档。"""
    adapter.model["P1"] = {
        "U4": {"minX": 1000, "minY": 480, "maxX": 1100, "maxY": 580},
        "C5": {"minX": 1500, "minY": 460, "maxX": 1540, "maxY": 500},
        "B1": {"minX": 600, "minY": 320, "maxX": 1250, "maxY": 435},
        "B2": {"minX": 900, "minY": 580, "maxX": 1250, "maxY": 640},
        "RB": {"minX": 1130, "minY": 440, "maxX": 1290, "maxY": 520},
    }
    adapter.pins_by_page["P1"] = {
        "U4": [{"pinNumber": "1", "x": 1050, "y": 480, "net": "3V3"},
               {"pinNumber": "2", "x": 1100, "y": 530, "net": "5V"},
               {"pinNumber": "3", "x": 1050, "y": 580, "net": "GND"}],
        "C5": [{"pinNumber": "1", "x": 1500, "y": 480, "net": "5V"},
               {"pinNumber": "2", "x": 1540, "y": 480, "net": "GND"}],
    }
    adapter.netports["P1"] = [
        {"primitiveId": "mc5", "componentType": "netport", "net": "5V",
         "x": 1470, "y": 480},
    ]
    if with_rf:
        adapter.model["P1"]["RF"] = {"minX": 876, "minY": 476, "maxX": 884, "maxY": 484}
        adapter.pins_by_page["P1"]["RF"] = [
            {"pinNumber": "1", "x": 880, "y": 480, "net": "X_N9"}]


def test_repack_pull_declines_slot_touching_foreign_conductor(tmp_path) -> None:
    """拉近终位电气触点门(run-b2c1990f44a4 相位快照哨定性:post-closeout
    五页 GND 全在 → post-compact 全灭,并轨源=_pull_long_pairs 移动):最优
    空位 (910,480) 的标记落点 (880,480) 压上 RF 异网脚——共端点即并网且
    网名粘死,守卫拒之;次优 (860,480) 被 RF 箱挡 → 本轮干脆不拉(安全降级:
    保 netport 走线,线长门兜底),绝不制造并网触点。"""
    chat = FakeChat("{}")
    adapter = _PullFakeAdapter("pass", [])
    _pull_guard_scene(adapter, with_rf=True)
    lc = LoopController(_ir_with_rails(("12V", 12.0)), _catalog(),
                        _candidates, chat, adapter, AuditLog(str(tmp_path)))
    n = lc._pull_long_pairs("P1", 1, {"ldo4": ["U4", "C5"]})
    assert n == 0
    # 没有任何移动/建组动作;C5 原地
    assert not [c for c in adapter.calls if c[:2] == ["sch", "group-move"]]
    p1 = next(p for p in adapter.pins_by_page["P1"]["C5"] if p["pinNumber"] == "1")
    assert (p1["x"], p1["y"]) == (1500, 480)
    assert not [e for e in _audit_events(str(tmp_path))
                if e.get("kind") == "pull-close"]


def test_repack_pull_clean_scene_takes_nearest_slot(tmp_path) -> None:
    """对照:无 RF 时同一场景取 90 档最优空位 (910,480)——证明上例的推远是
    电气门所致,不是场景本身无解。"""
    chat = FakeChat("{}")
    adapter = _PullFakeAdapter("pass", [])
    _pull_guard_scene(adapter, with_rf=False)
    lc = LoopController(_ir_with_rails(("12V", 12.0)), _catalog(),
                        _candidates, chat, adapter, AuditLog(str(tmp_path)))
    n = lc._pull_long_pairs("P1", 1, {"ldo4": ["U4", "C5"]})
    assert n == 1
    p1 = next(p for p in adapter.pins_by_page["P1"]["C5"] if p["pinNumber"] == "1")
    assert (p1["x"], p1["y"]) == (910, 480)


def test_mark_side_guard_reseats_opposite_side_markers(tmp_path) -> None:
    """标记同侧扫尾(run-cbdfa6d997bf 终态 199 检 10 违例——reseat fallback
    盲落标记锚在引脚对侧,桩线横穿本体,用户规范「连线不得穿越器件本体」):
    对侧独占标记拆掉按外侧优先序重落(AMS1117 pin2 形态:左脚右标 → 左重
    落);共享标记(一旗侍二脚)不动;同侧/垂直侧不触发。"""
    chat = FakeChat("{}")
    adapter = _RepackFakeAdapter("pass", [])
    adapter.model["P1"] = {
        "UA": {"minX": 400, "minY": 500, "maxX": 500, "maxY": 560},
        "UB": {"minX": 700, "minY": 500, "maxX": 800, "maxY": 560},
    }
    adapter.pins_by_page["P1"] = {
        "UA": [{"pinNumber": "1", "x": 395, "y": 530, "net": "A_N1"},
               {"pinNumber": "2", "x": 505, "y": 530, "net": "B_N2"}],
        "UB": [{"pinNumber": "1", "x": 695, "y": 530, "net": "A_N1"},
               {"pinNumber": "2", "x": 805, "y": 530, "net": "GND"}],
    }
    adapter.netports["P1"] = [
        # UA:1 左脚但标记在右侧体内对侧(穿体桩);UA:2 右脚右标=同侧合法;
        # UB:1 的 A_N1 标记离 UA:1 更远(共享判定按各自最近脚算,均独占)
        {"primitiveId": "m1", "componentType": "netport", "net": "A_N1",
         "x": 535, "y": 530},
        {"primitiveId": "m2", "componentType": "netport", "net": "B_N2",
         "x": 535, "y": 545},
        {"primitiveId": "m3", "componentType": "netport", "net": "A_N1",
         "x": 665, "y": 530},
        {"primitiveId": "m4", "componentType": "netflag", "net": "GND",
         "x": 835, "y": 530},
    ]
    lc = LoopController(_ir_with_rails(("12V", 12.0)), _catalog(),
                        _candidates, chat, adapter, AuditLog(str(tmp_path)))
    lc._fix_wrong_side_marks("P1", 1)
    evs = _audit_events(str(tmp_path))
    ev = next(e for e in evs if e.get("kind") == "mark-side-guard")
    assert ev["wrongside"] == 1
    assert ev["fixed"] == ["UA:1:A_N1->left/30"]
    # UA:1 拆旧桩+左向外侧重落;其余三个标记原样(同侧/独占不触发)
    assert {c[c.index("--pin") + 1] for c in adapter.calls
            if c[:2] == ["sch", "disconnect"]} == {"UA:1"}
    cons = [(c[1], c[4], c[5]) for c in adapter.connects if c[0] == "P1"]
    assert cons == [("UA:1", "left", 30.0)]
    new_mark = next(f for f in adapter.netports["P1"]
                    if str(f.get("primitiveId", "")).startswith("cn_")
                    and f["net"] == "A_N1")
    assert (new_mark["x"], new_mark["y"]) == (365, 530)


def test_repack_rail_pull_groupmove_netloss_restub(tmp_path) -> None:
    """rail 拉移 group-move 后单脚丢网(run-0cdc61dd3eea P2:C1 拉移后 2 脚 GND
    空、同趟 C3 双脚完好——共享 rail 网旗/连线没跟着走,不能假设 group-move
    保网):拉后回读比对,丢网脚审计 pull-netloss 并按新位重落。"""
    chat = FakeChat("{}")
    adapter = _PullFakeAdapter("pass", [])
    adapter.model["P1"] = {
        "U4": {"minX": 1000, "minY": 500, "maxX": 1100, "maxY": 600},
        "C5": {"minX": 1500, "minY": 500, "maxX": 1540, "maxY": 540},
        "C6": {"minX": 1000, "minY": 200, "maxX": 1040, "maxY": 240},
        "C7": {"minX": 1120, "minY": 620, "maxX": 1160, "maxY": 660},
        "R9": {"minX": 1200, "minY": 280, "maxX": 1240, "maxY": 320},
    }
    adapter.pins_by_page["P1"] = {
        "U4": [{"pinNumber": "1", "x": 1000, "y": 530, "net": "3V3"},
               {"pinNumber": "2", "x": 1100, "y": 530, "net": "5V"},
               {"pinNumber": "3", "x": 1050, "y": 600, "net": "GND"}],
        "C5": [{"pinNumber": "1", "x": 1500, "y": 530, "net": "5V"},
               {"pinNumber": "2", "x": 1540, "y": 530, "net": "GND"}],
        "C6": [{"pinNumber": "1", "x": 1000, "y": 220, "net": "3V3"},
               {"pinNumber": "2", "x": 1040, "y": 220, "net": "GND"}],
        "C7": [{"pinNumber": "1", "x": 1120, "y": 640, "net": "GND"},
               {"pinNumber": "2", "x": 1160, "y": 640, "net": "GND"}],
        "R9": [{"pinNumber": "1", "x": 1200, "y": 300, "net": "LDO4_N1"},
               {"pinNumber": "2", "x": 1240, "y": 300, "net": "LDO4_N1"}],
    }
    adapter.group_move_netloss = {"C5": ["2"]}  # 只有 C5 丢 GND(C6 同趟完好)
    lc = LoopController(_ir_with_rails(("12V", 12.0), ("3V3", 3.3)), _catalog(),
                        _candidates, chat, adapter, AuditLog(str(tmp_path)))
    n = lc._pull_long_pairs("P1", 1, {"ldo4": ["U4", "C5", "C6", "C7", "R9"]})
    assert n == 2
    evs = _audit_events(str(tmp_path))
    ev = next(e for e in evs if e.get("kind") == "pull-netloss")
    assert (ev["page"], ev["designator"], ev["pins"]) == ("P1", "C5", ["C5:2"])
    # 丢网脚重落(GND 回填;确定性 connect 或 autoconnect 兜底皆可),完好的
    # C6 不重落
    restubbed = ([c for c in adapter.connects if c[1] == "C5:2"]
                 + [a for a in adapter.autoconnects if a[1] == "C5:2"])
    assert restubbed
    assert not [c for c in adapter.connects if c[1].startswith("C6:")]
    assert not [a for a in adapter.autoconnects if a[1].startswith("C6:")]
    pin2 = next(p for p in adapter.pins_by_page["P1"]["C5"] if p["pinNumber"] == "2")
    assert pin2["net"] == "GND" and pin2["x"] < 1500  # 新位重落,不是原位回滚


def test_repack_rail_pull_neighbor_disconnect_tears_shared_netflag(tmp_path) -> None:
    """拉移丢网第二形态(run-86f0ec3ab850 P2:C10 group-move 后自身回读仍带
    网,后续邻居 C11 的 modify 回退 disconnect 拆掉共享 GND 网旗,把 C10:2
    拖成孤儿——单件即时校验抓不到):整趟 rail 拉移结束后统一回读全部拉移件
    比对补桩。"""
    chat = FakeChat("{}")
    adapter = _PullFakeAdapter("pass", [])
    adapter.model["P1"] = {
        "U4": {"minX": 1000, "minY": 500, "maxX": 1100, "maxY": 600},
        "C5": {"minX": 1500, "minY": 500, "maxX": 1540, "maxY": 540},
        "C6": {"minX": 1000, "minY": 200, "maxX": 1040, "maxY": 240},
        "C7": {"minX": 1120, "minY": 620, "maxX": 1160, "maxY": 660},
        "R9": {"minX": 1200, "minY": 280, "maxX": 1240, "maxY": 320},
    }
    adapter.pins_by_page["P1"] = {
        "U4": [{"pinNumber": "1", "x": 1000, "y": 530, "net": "3V3"},
               {"pinNumber": "2", "x": 1100, "y": 530, "net": "5V"},
               {"pinNumber": "3", "x": 1050, "y": 600, "net": "GND"}],
        "C5": [{"pinNumber": "1", "x": 1500, "y": 530, "net": "5V"},
               {"pinNumber": "2", "x": 1540, "y": 530, "net": "GND"}],
        "C6": [{"pinNumber": "1", "x": 1000, "y": 220, "net": "3V3"},
               {"pinNumber": "2", "x": 1040, "y": 220, "net": "GND"}],
        "C7": [{"pinNumber": "1", "x": 1120, "y": 640, "net": "GND"},
               {"pinNumber": "2", "x": 1160, "y": 640, "net": "GND"}],
        "R9": [{"pinNumber": "1", "x": 1200, "y": 300, "net": "LDO4_N1"},
               {"pinNumber": "2", "x": 1240, "y": 300, "net": "LDO4_N1"}],
    }
    # C6 的 group-move 被拒 → modify 回退;回退拆 C6:2 的 GND 桩时连带拆掉
    # 与 C5:2 共享的 GND 网旗(C5 此时已 group-move 完、即时回读是好的)
    adapter.group_move_fail_members = {"C6"}
    adapter.disconnect_tears = {"C6:2": ["C5:2"]}
    lc = LoopController(_ir_with_rails(("12V", 12.0), ("3V3", 3.3)), _catalog(),
                        _candidates, chat, adapter, AuditLog(str(tmp_path)))
    n = lc._pull_long_pairs("P1", 1, {"ldo4": ["U4", "C5", "C6", "C7", "R9"]})
    assert n == 2
    # 整趟后置校验:C5:2 被邻居拖丢 → 审计 + 重落;C6 自身由 modify 回退重落
    evs = _audit_events(str(tmp_path))
    ev = next(e for e in evs if e.get("kind") == "pull-netloss")
    assert (ev["designator"], ev["pins"]) == ("C5", ["C5:2"])
    restubbed5 = ([c for c in adapter.connects if c[1] == "C5:2"]
                  + [a for a in adapter.autoconnects if a[1] == "C5:2"])
    assert restubbed5
    pin5 = next(p for p in adapter.pins_by_page["P1"]["C5"] if p["pinNumber"] == "2")
    pin6 = next(p for p in adapter.pins_by_page["P1"]["C6"] if p["pinNumber"] == "2")
    assert pin5["net"] == "GND" and pin6["net"] == "GND"


def test_freeze_pack_reseat_escape_marks(tmp_path) -> None:
    """越带桩重落(run-fc264cf3ac76 目检:ULNST4 的 GND/VIN 兜底桩垂到
    y=-26 出图):marker 锚逃出 BAND → 按共轴同网最近脚归属,disconnect +
    sch connect 确定性重落(方向=离带边远侧,翼展入带);配不上主脚的跳过。"""
    chat = FakeChat("{}")
    adapter = _RepackFakeAdapter("pass", [])
    adapter.model["P1"] = {"ULN4": {"minX": 865, "minY": 195, "maxX": 945, "maxY": 265}}
    adapter.pins_by_page["P1"] = {"ULN4": [
        {"pinNumber": "8", "x": 865, "y": 195, "net": "GND"},
        {"pinNumber": "9", "x": 945, "y": 195, "net": "VIN"},
    ]}
    adapter.netports["P1"] = [
        {"primitiveId": "e1", "componentType": "netflag", "net": "GND", "x": 865, "y": -15},
        {"primitiveId": "e2", "componentType": "netflag", "net": "VIN", "x": 945, "y": -10},
        {"primitiveId": "e3", "componentType": "netport", "net": "GHOST", "x": -50, "y": 400},
        # run-885b01f68b1f 病灶:锚 70 在带内,ST2_IN2 横排文字 ~94 宽伸到 -24
        {"primitiveId": "e4", "componentType": "netport", "net": "ST2_IN2",
         "x": 70, "y": 120},
    ]
    adapter.model["P1"]["J1"] = {"minX": 110, "minY": 100, "maxX": 210, "maxY": 150}
    adapter.pins_by_page["P1"]["J1"] = [
        {"pinNumber": "2", "x": 125, "y": 120, "net": "ST2_IN2"}]
    lc = LoopController(_ir_with_rails(("12V", 12.0)), _catalog(), _candidates,
                        chat, adapter, AuditLog(str(tmp_path)))
    lc._reseat_escape_marks("P1", 1)
    evs = _audit_events(str(tmp_path))
    ev = next(e for e in evs if e.get("kind") == "freeze-pack-reseat")
    assert {r["pin"] for r in ev["reseated"]} == {"ULN4:8", "ULN4:9", "J1:2"}
    # 落桩带本体避让(2026-08-31 RC1a)+ 方向=引脚外侧优先(2026-09-01
    # 「连线不得穿越器件本体」):ULN4:8 角点脚 gap left=0 → left/30;ULN4:9
    # 右缘脚 outward=right(旧行为 up——标记落顶侧,桩线沿缘)→ right/30
    # 标记出右缘;J1:2 脚在自件边内(左 15)outward=left 全带外(文字翼展
    # ~94),顺边 up/30 出顶——桩程最短的一侧,不再横穿 90
    assert [(r["pin"], r["dir"], r["off"]) for r in ev["reseated"]] == [
        ("ULN4:8", "left", 30), ("ULN4:9", "right", 30), ("J1:2", "up", 30)]
    assert ev["skipped"] == ["GHOST@-50,400:no-pin"] and ev["failed"] == []
    assert {c[c.index("--pin") + 1] for c in adapter.calls
            if c[:2] == ["sch", "disconnect"]} == {"ULN4:8", "ULN4:9", "J1:2"}
    # 逃逸标记删除,重落标记在带内且不压本体:GND left/30 锚 (835,195);
    # VIN right/30 锚 (975,195)(右缘脚出右缘);ST2_IN2 up/30 锚 (125,150)
    acs = {f["net"]: f for f in adapter.netports["P1"]
           if str(f.get("primitiveId", "")).startswith("cn_")}
    assert not [f for f in adapter.netports["P1"]
                if str(f.get("primitiveId", "")).startswith("e")
                and f.get("net") in ("GND", "VIN", "ST2_IN2")]
    assert (acs["GND"]["x"], acs["GND"]["y"]) == (835, 195)
    assert (acs["VIN"]["x"], acs["VIN"]["y"]) == (975, 195)
    assert (acs["ST2_IN2"]["x"], acs["ST2_IN2"]["y"]) == (125, 150)
    assert all(30 <= f["x"] <= 1140 and 30 <= f["y"] <= 795 for f in acs.values())


def test_connect_stub_orders_directions_by_pin_side(tmp_path) -> None:
    """桩方向序=引脚外侧优先(2026-09-01 用户规范「连线不得穿越器件本体」:
    run-7db9b9f61430 目检 AMS1117 pin2 左缘而 3V3 旗钉本体正中、TPS54360
    pin9 顶缘而 GND 落下侧——「离带边最远」的方向序与引脚侧无关,天然产出
    穿体桩)。直接驱动 _connect_stub:左缘脚选 left、顶缘脚选 up;外侧被
    邻件墨迹全堵才顺边(perp 按带边空间);外侧+顺边全灭才对侧垫底——
    offset 拉长到标记离自件本体,宁可对侧长桩也不退 planner 盲落。"""
    chat = FakeChat("{}")
    adapter = _RepackFakeAdapter("pass", [])
    adapter.pins_by_page["P1"] = {
        d: [{"pinNumber": p, "x": x, "y": y, "net": ""}]
        for d, p, x, y in [("U9", "3", 495, 530), ("T1", "9", 550, 565),
                           ("U8", "1", 495, 530), ("U7", "1", 495, 530),
                           ("U6", "1", 495, 530)]}
    lc = LoopController(_ir_with_rails(("VIN", 12.0)), _catalog(), _candidates,
                        chat, adapter, AuditLog(str(tmp_path)))
    body = (500, 500, 600, 560)  # 本体;脚长在边上(引线外伸 5)
    # ① 左缘脚 → 标记出左缘
    assert lc._connect_stub("U9:3", "netport", "M1_IN_A", 495, 530,
                            [(555, 530)], body_rects=[body],
                            own_body=body) == ("left", 30)
    # ② 顶缘脚(TPS54360 pin9 同形)→ 标记出顶缘
    assert lc._connect_stub("T1:9", "netport", "M1_IN_B", 550, 565,
                            [(550, 530)], body_rects=[body],
                            own_body=body) == ("up", 30)
    # ③ 外侧(左)被邻件墨迹全堵(30~210 全压)→ 顺边:perp 按带边空间
    # down(500) > up(265)
    assert lc._connect_stub("U8:1", "netport", "M1_IN_C", 495, 530, [],
                            body_rects=[body, (150, 510, 470, 550)],
                            own_body=body) == ("down", 30)
    # ④ 外侧+顺边全堵(左邻墨迹+上/下腿障)→ 对侧垫底:120 才让标记墨迹
    # 离自件本体(30/60/90 压自件只进 fallback 不成选)
    assert lc._connect_stub("U7:1", "netport", "M1_IN_D", 495, 530, [],
                            body_rects=[body, (150, 510, 470, 550),
                                        (350, 545, 650, 700), (350, 300, 650, 505)],
                            own_body=body) == ("right", 120)
    # ⑤ 电气端点避让(run-30c3833705a4 P4:GND 旗与盲退 C7_N4 同锚 (910,460)
    # → 全局并网):外侧轴上有他标记锚 (465,530)——left/30 锚盘撞点、更长桩
    # 的桩线段又贴过该点,全 left 拒 → 顺边 down/30
    assert lc._connect_stub("U6:1", "netport", "M1_IN_E", 495, 530, [],
                            body_rects=[body], own_body=body,
                            avoid_pts=[(465, 530)]) == ("down", 30)
    # 方向序落到真调用:只有成选候选才发 connect,①-⑤ 逐条对上
    assert [c[c.index("--direction") + 1] for c in adapter.calls
            if c[:2] == ["sch", "connect"]] == ["left", "up", "down", "right", "down"]


def test_connect_stub_extended_offset_tier(tmp_path) -> None:
    """扩避让档(2026-09-01 布局治本):避体档 210→330——外侧短中档的标记
    墨迹全擦右侧偏轴邻件(leg 不穿体,仅 ±14 墨迹带内压体)时,270 档锚点+
    翼展整体越过邻件,确定性重落成选。旧档位同形只能吃 min-overlap 兜底
    (150/210 压叠落桩)或退 planner 盲落——重跑#2 fallback 94-110 枚/run 的
    主因就是长档缺位。"""
    chat = FakeChat("{}")
    adapter = _RepackFakeAdapter("pass", [])
    adapter.pins_by_page["P1"] = {
        "U5": [{"pinNumber": "1", "x": 495, "y": 530, "net": ""}]}
    lc = LoopController(_ir_with_rails(("VIN", 12.0)), _catalog(), _candidates,
                        chat, adapter, AuditLog(str(tmp_path)))
    body = (500, 500, 600, 560)  # 自件;脚在左缘(引线外伸 5)
    # 左/上/下被邻件 leg 封死(同 orders 测试夹具);右侧偏轴邻件
    # (620..760, y 517..527)贴在桩走廊上缘:leg(y=530)不穿,但 120~210 档
    # 锚点+166 翼展(LONGNETNAME12 13 字)墨迹全压它,270 档锚 765 翼到 931 越过
    assert lc._connect_stub("U5:1", "netport", "LONGNETNAME12", 495, 530, [],
                            body_rects=[body, (150, 510, 470, 550),
                                        (350, 545, 650, 700), (350, 300, 650, 505),
                                        (620, 517, 760, 527)],
                            own_body=body) == ("right", 270)
    # 唯一成选:先净后落(压叠候选只进 fallback 不发 connect)
    assert [c[c.index("--direction") + 1] for c in adapter.calls
            if c[:2] == ["sch", "connect"]] == ["right"]


def test_reseat_blind_guard_audits_bad_fallback(tmp_path) -> None:
    """盲退质量关(reseat 侧,unguarded 路径):全围死脚 _connect_stub 耗尽
    → planner 盲落进邻件本体 → 护栏检出、拆脚确定性重试(仍围死)、重退
    planner 保连通——翼擦从「静默」变「计数」(§10 软指标),连通不破。"""
    chat = FakeChat("{}")
    adapter = _RepackFakeAdapter("pass", [])
    adapter.model["P1"] = {
        "U1": {"minX": 505, "minY": 500, "maxX": 600, "maxY": 560},
        "LB": {"minX": 100, "minY": 510, "maxX": 497, "maxY": 550},
        "RB": {"minX": 503, "minY": 510, "maxX": 900, "maxY": 555},
        "UB": {"minX": 400, "minY": 533, "maxX": 700, "maxY": 900},
        "DB": {"minX": 400, "minY": 100, "maxX": 700, "maxY": 527},
    }
    adapter.pins_by_page["P1"] = {"U1": [
        {"pinNumber": "1", "x": 500, "y": 530, "net": "NETX"}]}
    adapter.netports["P1"] = [
        {"primitiveId": "e1", "componentType": "netport", "net": "NETX",
         "x": 700, "y": 545}]  # 压 RB 本体 → 判据② 触发 reseat
    adapter.ac_marks["P1"] = [{"net": "NETX", "dx": 200, "dy": 15}]  # 盲落恒回坏点
    lc = LoopController(_ir_with_rails(("12V", 12.0)), _catalog(), _candidates,
                        chat, adapter, AuditLog(str(tmp_path)))
    lc._reseat_escape_marks("P1", 1)
    evs = _audit_events(str(tmp_path))
    ev = next(e for e in evs if e.get("kind") == "freeze-pack-reseat")
    assert [r["pin"] for r in ev["reseated"]] == ["U1:1"]
    assert ev["reseated"][0]["dir"] == "auto"
    assert ev["failed"] == ["U1:1:NETX:fallback"]
    g = next(e for e in evs if e.get("kind") == "reseat-blind-guard")
    assert (g["outcome"], g["pin"], g["bad_drop"]) == ("unguarded", "U1:1", "700,545")
    # 盲落两回(初落+重落),拆脚两回(reseat+护栏重试前);connect 零发(全围死)
    assert sum(1 for c in adapter.calls if c[:2] == ["sch", "autoconnect"]) == 2
    assert sum(1 for c in adapter.calls if c[:2] == ["sch", "disconnect"]) == 2
    assert not [c for c in adapter.calls if c[:2] == ["sch", "connect"]]
    assert not lc._wire_breaks
    # 终态:只剩重落的盲标记(坏但连通,已计数交目检),e1 已随 disconnect 清除
    assert [(f["net"], f["x"], f["y"]) for f in adapter.netports["P1"]] == \
        [("NETX", 700.0, 545.0)]


def test_blind_guard_reseats_bad_marker_deterministically(tmp_path) -> None:
    """盲退质量关(reguard 路径):盲落标记压自件本体 → 护栏拆脚按新几何
    确定性重落成选(left/30,带内离体),avoid_pts live 追加新锚,审计
    outcome=reguard;盲落标记随 disconnect 清除,终态只有确定性标记。"""
    chat = FakeChat("{}")
    adapter = _RepackFakeAdapter("pass", [])
    adapter.model["P1"] = {"U1": {"minX": 505, "minY": 500, "maxX": 600, "maxY": 560}}
    adapter.pins_by_page["P1"] = {"U1": [
        {"pinNumber": "1", "x": 500, "y": 530, "net": ""}]}
    adapter.ac_marks["P1"] = [{"net": "NETX", "dx": 35, "dy": 10}]  # 盲落压自件本体
    lc = LoopController(_ir_with_rails(("12V", 12.0)), _catalog(), _candidates,
                        chat, adapter, AuditLog(str(tmp_path)))
    avoid_pts: list[tuple[float, float]] = []
    st = lc._guarded_autoconnect("P1", 1, "U1:1", "netport", "NETX",
                                 500, 530, [], (505, 500, 600, 560),
                                 [(505, 500, 600, 560)], avoid_pts, tag="t-guard")
    assert st == "reguard" and avoid_pts == [(470, 530)]
    g = next(e for e in _audit_events(str(tmp_path)) if e.get("kind") == "t-guard")
    assert (g["outcome"], g["reseat"], g["bad_drop"]) == ("reguard", "left/30", "535,540")
    # 终态:确定性标记在带内离体(left/30 锚 (470,530)),盲落标记已清除
    assert [(f["primitiveId"], f["x"], f["y"]) for f in adapter.netports["P1"]] == \
        [("cn_U1:1", 470.0, 530.0)]


def test_mark_merge_guard_reseats_coincident_anchors(tmp_path) -> None:
    """标记端点并网护栏(run-30c3833705a4 P4 真机定性:盲退 C7_N4 netport 与
    已落 GND 旗**同锚异网** (910,460) → 电源网 isGlobal 一点并轨,GND↔5V
    全局短接、四页 GND 齐灭,且并网后网对象粘死、修复重落也回不来)。
    护栏重列标记,同锚异网/锚压异网脚端点 → 拆配对脚(先同网配、几何兜底)
    带电气端点避让确定性重落,新锚分离,全程入审计。"""
    chat = FakeChat("{}")
    adapter = _RepackFakeAdapter("pass", [])
    adapter.model["P1"] = {
        "UA": {"minX": 400, "minY": 500, "maxX": 500, "maxY": 560},
        "UB": {"minX": 560, "minY": 500, "maxX": 660, "maxY": 560},
    }
    adapter.pins_by_page["P1"] = {
        "UA": [{"pinNumber": "1", "x": 395, "y": 530, "net": "GND"}],
        "UB": [{"pinNumber": "1", "x": 555, "y": 530, "net": "C7_N9"}],
    }
    # 对脸双引脚(395/555)各出 30 桩正撞中点 (475,530) —— 同锚异网
    adapter.netports["P1"] = [
        {"primitiveId": "m1", "componentType": "netflag", "net": "GND",
         "x": 475, "y": 530},
        {"primitiveId": "m2", "componentType": "netport", "net": "C7_N9",
         "x": 475, "y": 530},
    ]
    lc = LoopController(_ir_with_rails(("VIN", 12.0)), _catalog(), _candidates,
                        chat, adapter, AuditLog(str(tmp_path)))
    lc._fix_marker_coincidences("P1", 1)
    ev = next(e for e in _audit_events(str(tmp_path))
              if e.get("kind") == "mark-merge-guard")
    assert ev["coincident"] == 2 and ev["failed"] == []
    # 两枚都拆脚确定性重落:GND 出左缘 left/30;C7_N9 左向被旧锚/墨迹全堵
    # (桩线贴过旧锚=端点并网要拒)顺边 down/30——新锚分离
    assert ev["fixed"] == ["UA:1:GND@475,530->left/30",
                           "UB:1:C7_N9@475,530->down/30"]
    assert {c[c.index("--pin") + 1] for c in adapter.calls
            if c[:2] == ["sch", "disconnect"]} == {"UA:1", "UB:1"}
    acs = {f["net"]: (f["x"], f["y"]) for f in adapter.netports["P1"]
           if str(f.get("primitiveId", "")).startswith("cn_")}
    assert acs["GND"] != acs["C7_N9"]
    assert all(30 <= x <= 1140 and 30 <= y <= 795 for x, y in acs.values())


def test_freeze_net_repair_reroutes_rail_swallowed_pins(tmp_path) -> None:
    """缺网修复通道(run-5a2ddef8a563 定性:拉移后引脚挂**错网**——PMOSREV1:3
    =5V、ULNM3:4=GND、MCU1:21=3V3,reseat 只认「脚无网」是检测盲区):缺网
    页上规划绑定该网的脚,实测网≠规划网 → disconnect 删错网残桩 → 按计划网
    重落;对网脚不动;无规划脚的网(上游模板网)记 unverified 交目检。"""
    chat = FakeChat("{}")
    adapter = _RepackFakeAdapter("pass", [])
    adapter.model["P1"] = {"ULNB": {"minX": 560, "minY": 660, "maxX": 660, "maxY": 720}}
    adapter.pins_by_page["P1"] = {"ULNB": [
        # 真机病灶同形:应挂 M1_IN_D/VIN 的脚分别并进了 GND/5V 轨
        {"pinNumber": "4", "x": 660, "y": 700, "net": "GND"},
        {"pinNumber": "9", "x": 660, "y": 680, "net": "5V"},
        {"pinNumber": "8", "x": 560, "y": 700, "net": "GND"},  # 对网(GND 不缺)
    ]}
    adapter.nets["P1"] = {"GND": [], "5V": []}
    lc = LoopController(_ir_with_rails(("VIN", 12.0)), _catalog(), _candidates,
                        chat, adapter, AuditLog(str(tmp_path)))
    acts = [
        Action(kind="sch-autoconnect", block_instance="uln1",
               args=["sch", "autoconnect", "--pin", "ULN1:4",
                     "--kind", "netport", "--net", "M1_IN_D"], page="P1"),
        Action(kind="sch-autoconnect", block_instance="uln1",
               args=["sch", "autoconnect", "--pin", "ULN1:9",
                     "--kind", "power", "--net", "VIN"], page="P1"),
        Action(kind="sch-autoconnect", block_instance="uln1",
               args=["sch", "autoconnect", "--pin", "ULN1:8",
                     "--kind", "gnd", "--net", "GND"], page="P1"),
    ]
    missing = [{"page": "P1", "missing": ["M1_IN_D", "VIN", "C1_N2"]}]
    remaining = lc._repair_missing_nets(
        acts, 1, {"uln1": "P1"}, {"uln1": ("ULN1", "ULNB")}, missing,
        page_of=lambda a: "P1", pages=["P1"])
    # 换名翻译:动作里的 ULN1:* 落到实际位号 ULNB:*;只有错网脚被拆(8 号
    # GND 对网不动),错网脚按计划网重落
    assert {c[c.index("--pin") + 1] for c in adapter.calls
            if c[:2] == ["sch", "disconnect"]} == {"ULNB:4", "ULNB:9"}
    conns = {(p, n) for _pg, p, _k, n, _d, _o in adapter.connects}
    assert conns == {("ULNB:4", "M1_IN_D"), ("ULNB:9", "VIN")}
    pins = {p["pinNumber"]: p["net"] for p in adapter.pins_by_page["P1"]["ULNB"]}
    assert pins["4"] == "M1_IN_D" and pins["9"] == "VIN" and pins["8"] == "GND"
    ev = next(e for e in _audit_events(str(tmp_path)) if e.get("kind") == "net-repair")
    assert sorted(ev["repaired"]) == ["ULNB:4->M1_IN_D", "ULNB:9->VIN"]
    assert ev["unverified"] == [{"page": "P1", "nets": ["C1_N2"],
                                 "why": "no-planned-pin(upstream 模板网?)"}]
    # 复检:三网(含 GND)在页网表都有载体,余缺清零
    assert remaining == []


def test_repack_compacts_internal_nets(tmp_path) -> None:
    """块内网紧凑化(先拆后画):disconnect --pin 删桩+netport → wire --net
    直连;跨块网/几何不通的网保留 netport 不动。"""
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _RepackFakeAdapter("pass", ["dcin1", "dcin2"])
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    evs = _audit_events(str(tmp_path))
    comp = [e for e in evs if e.get("kind") == "compact-nets"]
    assert comp, "试放相位应有紧凑化审计"
    p1 = [e for e in comp if e.get("page") == "P1"]
    assert p1 and p1[0].get("converted"), "两块各一条内部网都应转换"
    assert adapter.wires, "应画出直连折线"
    for _pg, pts_json, net in adapter.wires:
        pts = json.loads(pts_json)
        assert net.startswith("X") and "_N" in net
        assert all(len(p) == 2 for p in pts) and len(pts) >= 2
    # 顺序防线:先拆(--pin,此时脚上只有桩)后画——先画会被"共端点即并线"
    # 合并成折返多段线,--pin 就会把直连线一起删(真机 run-9e1c0a4e08d3)
    first_wire = next(i for i, c in enumerate(adapter.calls) if c[:2] == ["sch", "wire"])
    first_disc = next(i for i, c in enumerate(adapter.calls) if c[:2] == ["sch", "disconnect"])
    assert first_disc < first_wire
    assert all("--pin" in c for c in adapter.calls if c[:2] == ["sch", "disconnect"])
    # 翻页先行:disconnect/wire 作用于活动页,前一条调用必须是 sch open --page
    assert adapter.calls[first_disc - 1][:2] == ["sch", "open"]
    # 拆掉的 netport 从数据面消失
    assert not adapter.netports.get("P1"), "P1 的内部网 netport 应全部拆掉"
    # 生产相位(收口前)对正式页再紧凑化一次
    assert len([e for e in comp if e.get("page") == "P1"]) >= 2 or \
        any(e.get("page") != "P1" for e in comp)
    # 紧凑化不改变主链审计
    for ev in ("repack-trial", "repack-measure", "repack-pack"):
        assert ev in [e.get("kind") for e in evs]


def test_repack_compact_wire_failure_reconnects(tmp_path) -> None:
    """直连线画失败 → autoconnect 把脚桩+netport 原样接回(不留断网),
    该网记 kept=wire-failed。"""
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _RepackFakeAdapter("pass", ["dcin1", "dcin2"], fail_wires=True)
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    evs = _audit_events(str(tmp_path))
    comp = [e for e in evs if e.get("kind") == "compact-nets" and e.get("page") == "P1"]
    assert comp and not comp[0].get("converted")
    assert all("wire-failed" in v for v in comp[0].get("kept", {}).values())
    # 每个拆过的脚都被 autoconnect 接回原网
    assert adapter.disconnects, "先拆仍会发生"
    recon = {d.split(":", 1)[0] for _pg, d, _n in adapter.autoconnects}
    cut = {d.split(":")[1] for d in adapter.disconnects if d.startswith("P1:")}
    assert cut and cut <= recon


class _RestoreFailAdapter(_RepackFakeAdapter):
    """恢复路径 autoconnect 可控失败:fail_until_replay=True 时仅试放相位
    (block-apply 计数 ≤ 试放块数,即重放尚未开始)失败,生产相位照常成功;
    fail_ac_pins 只拒指定 D:P(其余恢复成功)——单脚失败形态(网另有载体)。"""

    def __init__(self, *a, fail_wires: bool = True, fail_until_replay: bool = False,
                 fail_ac_pins: set[str] | None = None, **kw) -> None:
        super().__init__(*a, fail_wires=fail_wires, **kw)
        self.fail_until_replay = fail_until_replay
        self.fail_ac_pins = fail_ac_pins or set()
        self.ba_count = 0

    def run(self, args):
        if args[:2] == ["sch", "block-apply"]:
            self.ba_count += 1
        if args[:2] == ["sch", "autoconnect"] and "--pin" in args:
            pin_ref = args[args.index("--pin") + 1]
            if self.fail_ac_pins:
                # 定向模式:只拒名单内脚,其余恢复成功(单脚失败、网另有载体)
                if pin_ref in self.fail_ac_pins:
                    return 1, "{}", "fake: restore refused"
            elif not self.fail_until_replay or self.ba_count <= len(self.instances):
                return 1, "{}", "fake: restore refused"
        return super().run(args)


def test_repack_wire_restore_broken_halts(tmp_path) -> None:
    """wire 与恢复 autoconnect 双败 → 断网计数 >3(两块各 1 网 × 2 脚 = 4)
    → WIRE_RESTORE_BROKEN error(RELAYOUT),复跑同形 → 同码连胜 HALT。"""
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _RestoreFailAdapter("pass", ["dcin1", "dcin2"])
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "HALT"
    codes = [f.code for r in result.rounds for f in r.findings if not f.weak]
    assert "WIRE_RESTORE_BROKEN" in codes
    # 对照:≤3 处/恢复成功分别是 WIRE_RESTORE_WEAK 弱告警与零告警
    # (test_repack_compact_wire_failure_reconnects 已覆盖)


def test_repack_trial_phase_breaks_do_not_gate_production(tmp_path) -> None:
    """试放相位的恢复失败只污染随即清弃的 P1 试放画布,不计入生产判据:
    重放前清零 _wire_breaks——恢复只在试放失败、生产照常接回 → PASS。"""
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _RestoreFailAdapter("pass", ["dcin1", "dcin2"], fail_until_replay=True)
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    assert not any(f.code in ("WIRE_RESTORE_BROKEN", "WIRE_RESTORE_WEAK")
                   for r in result.rounds for f in r.findings)


def test_repack_net_missing_zero_carrier_halts(tmp_path) -> None:
    """F0 e2e(内部网盲区):单块 wire+恢复双败只有 2 处(≤3,计数门放过),
    但内部网拆桩后零载体——收口前基线并入 planned,页网表(sch read nets)
    无此网 → NET_MISSING error(REWIRE)阻断。计数门之外的第二道硬门。"""
    chat = FakeChat(json.dumps(_PLAN_1BLK, ensure_ascii=False))
    adapter = _RestoreFailAdapter("pass", ["dcin1"])
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "HALT"
    codes = [f.code for r in result.rounds for f in r.findings if not f.weak]
    assert "NET_MISSING" in codes
    assert "WIRE_RESTORE_BROKEN" not in codes  # 2 处 ≤3:计数门不管,存在性门接住
    ev = next(f for r in result.rounds for f in r.findings if f.code == "NET_MISSING")
    # 单块:试放 apply_seq=0、重放 =1 → 内部网 X1_N1;bind 的 12V 落了页网表
    # 不在缺失名单——缺失的正是"动作流看不见、只剩基线通道"的内部网
    assert "X1_N1" in ev.evidence and "12V" not in ev.evidence


def test_repack_wire_restore_weak_net_survives(tmp_path) -> None:
    """≤3 处恢复失败且网另有载体:对脚回接成功即建网 → WIRE_RESTORE_WEAK
    弱告警 + PASS;NET_MISSING 不触发(存在性看网,不看脚)。"""
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    # 两块重放 apply_seq=2,3 → dcin1 重放成员 DCI2_1/DCI2_2,内部网 X2_N1
    adapter = _RestoreFailAdapter("pass", ["dcin1", "dcin2"], fail_ac_pins={"DCI2_2:1"})
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    assert "WIRE_RESTORE_WEAK" in [f.code for r in result.rounds for f in r.findings if f.weak]
    assert "NET_MISSING" not in [f.code for r in result.rounds for f in r.findings]
    assert "WIRE_RESTORE_BROKEN" not in [f.code for r in result.rounds for f in r.findings]


def test_production_module_frames_default_on(tmp_path) -> None:
    """生产(非 freeze)默认画模块框(v0.6.11 审计 P1):PASS 流程在正式页出
    trial-freeze 画框事件;框=volume 口径;时序在紧凑化/收口之后、gate 之前。"""
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _RepackFakeAdapter("pass", ["dcin1", "dcin2"])
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    evs = _audit_events(str(tmp_path))
    fz = [e for e in evs if e.get("kind") == "trial-freeze" and e.get("drawn")]
    assert fz, "生产相位应画模块框"
    assert fz[-1].get("page") == "P1" and fz[-1].get("blocks") == 2
    draw = [i for i, c in enumerate(adapter.calls) if c[:2] == ["debug", "exec"]]
    assert draw, "画框走 debug exec 原语"
    js = adapter.calls[draw[-1]][adapter.calls[draw[-1]].index("--code") + 1]
    assert js.count("#1E90FF") == 4  # 两块 × (矩形+标注) 各一处实测蓝
    # 时序:最后一趟生产紧凑化(autoconnect/wire)在画框前,gate 在画框后
    gate_i = [i for i, c in enumerate(adapter.calls) if c[:2] == ["sch", "gate"]]
    last_compact = max(
        (i for i, c in enumerate(adapter.calls)
         if c[:2] in (("sch", "wire"), ("sch", "autoconnect"))), default=-1)
    assert last_compact < draw[-1], "框须框住紧凑化后的最终墨迹"
    assert gate_i and draw[-1] < gate_i[0]


def test_production_module_frames_env_off(tmp_path, monkeypatch) -> None:
    """EDALOOP_LAYOUT_FRAMES=0 关闭生产画框:无 debug exec、无 trial-freeze。"""
    monkeypatch.setenv("EDALOOP_LAYOUT_FRAMES", "0")
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _RepackFakeAdapter("pass", ["dcin1", "dcin2"])
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    assert not [c for c in adapter.calls if c[:2] == ["debug", "exec"]]
    evs = _audit_events(str(tmp_path))
    assert not [e for e in evs if e.get("kind") in ("trial-freeze", "module-frames")]


class _DisorderPagesAdapter(_RepackFakeAdapter):
    """sch pages 按注入序回报(模拟真机乱序工程 [P1,P3,P2]);page-delete/
    page-rename 真改页表——驱动 _rebuild_page_order 的锚-删-建重建。"""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        # (name, uuid):真机实测形态——页签序乱,CLI 无 page-reorder
        self.page_rows = [("P1", "u1"), ("P3", "u3"), ("P2", "u2")]

    def run(self, args):
        if args[:2] == ["sch", "pages"]:
            self.calls.append(args)
            pages = [{"name": n, "uuid": u, "parentSchematicUuid": "s1"}
                     for n, u in self.page_rows]
            return 0, json.dumps({"result": {"pages": pages}}), ""
        if args[:2] == ["sch", "page-rename"]:
            self.calls.append(args)
            uid = args[args.index("--page") + 1]
            name = args[args.index("--name") + 1]
            rows = [(name if u == uid else n, u) for n, u in self.page_rows]
            if uid not in {u for _n, u in rows}:  # 新建页改名 = 追加到末尾
                rows.append((name, uid))
            self.page_rows = rows
            return 0, "{}", ""
        if args[:2] == ["sch", "page-delete"]:
            self.calls.append(args)
            self.page_rows = [(n, u) for n, u in self.page_rows
                              if u != args[args.index("--page") + 1]]
            return 0, "{}", ""
        return super().run(args)

    def run_json(self, args):
        if args[:2] in (["sch", "pages"], ["sch", "page-new"],
                        ["sch", "page-rename"], ["sch", "page-delete"]):
            _rc, out, _err = self.run(args)
            return json.loads(out or "{}")
        return super().run_json(args)


def test_repack_rebuilds_disordered_page_tabs(tmp_path) -> None:
    """P2 页序重建:工程页签乱序([P1,P3,P2])且三页全在本轮计划(不触发
    修剪)→ 幂等重建:锚页 P1 改临时名 → 删 P3/P2 → 按号升序重建 → 删锚;
    终态页签升序,三块各落一页。"""
    plan3 = json.loads(json.dumps(_PLAN_2WIDE))
    plan3["blocks"].append(dict(plan3["blocks"][0], instance="dcin3"))
    chat = FakeChat(json.dumps(plan3, ensure_ascii=False))
    # 660×350/块:同页第二行 y<30 → 三块三页(页序才进入 keep,不被修剪先删)
    members = {"dcin1": [(0, 0, 360, 350), (400, 0, 260, 350)],
               "dcin2": [(0, 0, 360, 350), (400, 0, 260, 350)],
               "dcin3": [(0, 0, 360, 350), (400, 0, 260, 350)]}
    adapter = _DisorderPagesAdapter("pass", ["dcin1", "dcin2", "dcin3"], members=members)
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    evs = _audit_events(str(tmp_path))
    ev = next(e for e in evs if e.get("kind") == "page-reorder")
    assert ev.get("old") == ["P1", "P3", "P2"]
    assert ev.get("created") == ["P1", "P2", "P3"]
    # 终态页签升序;三块经 --doc 钉扎分落三页
    assert [n for n, _u in adapter.page_rows] == ["P1", "P2", "P3"]
    applies = [c for c in adapter.calls if c[:2] == ["sch", "block-apply"]
               and c[c.index("--doc") + 1] in ("P1", "P2", "P3")]
    assert {c[c.index("--doc") + 1] for c in applies} == {"P1", "P2", "P3"}
    # 锚-删-建序列:P1 先改 __reorder_tmp__,P3/P2 逐个删,重建后删锚
    names_deleted = [c[c.index("--page") + 1] for c in adapter.calls
                     if c[:2] == ["sch", "page-delete"]]
    assert names_deleted[-1] == "u1"  # 临时锚(原 P1 的 uuid)最后删
    assert set(names_deleted[:-1]) == {"u2", "u3"}


def test_repack_compact_fixpoint_cascade(tmp_path) -> None:
    """不动点级联:N1 可直连;N2 同行两脚在第 1 轮被 N1 的标记/墙标记封死
    全部候选,第 2 轮(N1 的桩/标记按"会转换"移除后)直线解锁——单轮规划
    会把 N2 误判为 route-blocked。"""
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _RepackFakeAdapter("pass", ["dcin1", "dcin2"], cascade=True)
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    evs = _audit_events(str(tmp_path))
    p1 = next(e for e in evs
              if e.get("kind") == "compact-nets" and e.get("page") == "P1")
    assert "X0_N1" in p1["converted"]
    assert "X0_N2" in p1["converted"], "级联网应在第 2 轮解锁并转换"
    # N2 解锁后走的是两点直线(同行直连),不是绕行(试放相位线,看 wires_all)
    n2w = [w for w in adapter.wires_all if w[0] == "P1" and w[2] == "X0_N2"]
    assert n2w and len(json.loads(n2w[0][1])) == 2


def test_repack_compact_kept_net_reseats_tight(tmp_path) -> None:
    """route-blocked 保留网紧贴重落:直连线画完后把保留网的 block-apply 长桩
    拆掉,autoconnect --offset-max 40 收紧重落(真机 run-09fcc8639b15 J2.A7
    实测 265 长桩与他网直连线构成 10 间距长平行);cross-block 保留网不动。"""
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _RepackFakeAdapter("pass", ["dcin1", "dcin2"], blocked=True)
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    evs = _audit_events(str(tmp_path))
    p1 = next(e for e in evs
              if e.get("kind") == "compact-nets" and e.get("page") == "P1")
    assert "X0_N3" in p1["kept"]
    assert p1["kept"]["X0_N3"].startswith("route-blocked(reseated"), p1["kept"]["X0_N3"]
    # 重落调用:disconnect + autoconnect --offset-max 40 成对、网名钉对
    tight = [c for c in adapter.calls if c[:2] == ["sch", "autoconnect"]
             and "--offset-max" in c and c[c.index("--net") + 1] == "X0_N3"]
    assert tight and all(c[c.index("--offset-max") + 1] == "40" for c in tight)
    reseated_pins = {c[c.index("--pin") + 1] for c in tight}
    assert reseated_pins == {"DCI0_1:3", "DCI0_2:3"}
    # 每个重落脚先被拆过(顺序:拆 → 紧落)
    for pin in reseated_pins:
        d = pin.split(":")[0]
        assert any(x.startswith(f"P1:{d}:X0_N3") for x in adapter.disconnects)


class _PullFakeAdapter(_RepackFakeAdapter):
    """组系统 fake(P0-1 对伴拉近用):group create/ungroup/list 维护组表,
    group-move 刚移成员的本体框+引脚(几何状态机与真机 move 内核同口径)。
    group_move_rc=1 模拟移动内核拒移(纸边钳制形状);modify_rc=1 模拟 SDK
    改位通道也失败——双路皆败交线长门兜底。"""

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.groups: dict[str, list[str]] = {}  # gid -> [designator]
        self.gseq = 0
        self.group_move_rc = 0
        self.modify_rc = 0
        # group-move 成功后指定件丢指定脚的网(真机 run-0cdc61dd3eea P2:C1 拉
        # 移后 2 脚 GND 空、同趟 C3 双脚完好——共享 rail 网旗/连线没跟着走,
        # 时灵时不灵,不能假设 group-move 保网)
        self.group_move_netloss: dict[str, list[str]] = {}  # designator -> [pinNumber]
        # 指定成员的 group-move 被拒(其余成员照常成功)——制造"邻居走 modify
        # 回退"的混合路径
        self.group_move_fail_members: set[str] = set()

    def run(self, args):
        if args[:3] == ["sch", "group", "list"]:
            self.calls.append(args)
            page = args[args.index("--doc") + 1] if "--doc" in args else "P1"
            gs = [{"id": g, "members": [{"designator": m} for m in ms]}
                  for g, ms in self.groups.items()]
            return 0, json.dumps({"groupsByPage": {page: gs}}), ""
        if args[:3] == ["sch", "group", "create"]:
            self.calls.append(args)
            self.gseq += 1
            self.groups[f"g{self.gseq}"] = [
                m for m in args[args.index("--members") + 1].split(",") if m]
            return 0, "{}", ""
        if args[:3] == ["sch", "group", "ungroup"]:
            self.calls.append(args)
            self.groups.pop(args[args.index("--group") + 1], None)
            return 0, "{}", ""
        if args[:2] == ["sch", "group-move"]:
            self.calls.append(args)
            if self.group_move_rc or any(
                    m in self.group_move_fail_members for m in self.groups.get(
                        args[args.index("--group") + 1], [])):
                return self.group_move_rc or 1, "{}", \
                    "fake: 目标位撞图纸边界,本次未动"
            gid = args[args.index("--group") + 1]
            dx = float(args[args.index("--dx") + 1])
            dy = float(args[args.index("--dy") + 1])
            page = args[args.index("--doc") + 1] if "--doc" in args else "P1"
            for m in self.groups.get(gid, []):
                b = self.model.get(page, {}).get(m)
                if b:
                    for kk in ("minX", "maxX"):
                        b[kk] += dx
                    for kk in ("minY", "maxY"):
                        b[kk] += dy
                for p in self.pins_by_page.get(page, {}).get(m) or []:
                    if p.get("x") is not None:
                        p["x"] += dx
                        p["y"] += dy
                    if str(p.get("pinNumber")) in self.group_move_netloss.get(m, []):
                        p["net"] = ""
            return 0, "{}", ""
        if args[:2] == ["sch", "modify"] and "--x" in args:
            # 改位通道(拉近回退):--x/--y 是元件原点,fake 里原点=bbox 左下
            # (与 sch list 的 x,y 同口径)→ 整体平移本体框+引脚
            self.calls.append(args)
            if self.modify_rc:
                return self.modify_rc, "{}", "fake: modify boom"
            d = args[args.index("--id") + 1].removeprefix("pc_")
            page = args[args.index("--doc") + 1] if "--doc" in args else self.active_page
            nx = float(args[args.index("--x") + 1])
            ny = float(args[args.index("--y") + 1])
            b = self.model.get(page, {}).get(d)
            if b:
                mx, my = nx - b["minX"], ny - b["minY"]
                for kk, dv in (("minX", mx), ("maxX", mx), ("minY", my), ("maxY", my)):
                    b[kk] += dv
                for p in self.pins_by_page.get(page, {}).get(d) or []:
                    if p.get("x") is not None:
                        p["x"] += mx
                        p["y"] += my
            return 0, "{}", ""
        return super().run(args)


class _TruncListFakeAdapter(_RepackFakeAdapter):
    """sch list 的 --include-bbox 变体恒返 >900KB 垃圾(真机 1MB 管道截断形状:
    run-1dff13dad148 在 1048576 字符处断裂,json 必炸);无 bbox 变体正常返回
    (不带 bbox)——验证截断特征识别 + 降载重试 + 引脚凸包兜底。"""

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.bbox_attempts = 0

    def run(self, args):
        if args[:2] == ["sch", "list"]:
            if "--include-bbox" in args:
                self.bbox_attempts += 1
                return 0, "x" * 900_100, ""
            rc, out, err = super().run(args)
            rep = json.loads(out)
            for c in rep.get("result", {}).get("components", []):
                c.pop("bbox", None)
            return rc, json.dumps(rep), err
        return super().run(args)


class _ClearFailFakeAdapter(_RepackFakeAdapter):
    """sch read 永远回一个幽灵件(真机「clear 后 settle 期回读残件」的恒态化):
    清页复核两趟必败——试放画布验证与 run 级清页门禁双双触发。"""

    def run(self, args):
        if args[:2] == ["sch", "read"]:
            return 0, json.dumps(
                {"result": {"components": [{"designator": "GHOST1", "pins": []}]}}), ""
        return super().run(args)


def test_repack_compact_span_gate_keeps_long_net(tmp_path) -> None:
    """线长门(P0-1):内部网脚距 520 > 400 不硬转真线(netport 语义保留,与
    route-blocked 同走重落收紧);块内近距网(220)照常转换。基座 fake 无组
    状态 → 拉近注定失败,门是最后一道防线。"""
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _RepackFakeAdapter(
        "pass", ["dcin1", "dcin2"],
        members={"dcin1": [(0, 0, 300, 250), (650, 0, 300, 250)]})  # 脚距 370+150=520
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    # 落-量-清:compact 按实例逐块调用,kept/converted 分散在不同事件里;只看
    # 试放相位(repack-measure 之前)的 P1 事件聚合判
    evs = _audit_events(str(tmp_path))
    m_i = next(i for i, e in enumerate(evs) if e.get("kind") == "repack-measure")
    comps = [e for e in evs[:m_i]
             if e.get("kind") == "compact-nets" and e.get("page") == "P1"]
    x0 = [e for e in comps if "X0_N1" in e.get("kept", {})]
    assert x0 and x0[0]["kept"]["X0_N1"] == "too-long(reseated2)"
    assert any("X1_N1" in e.get("converted", {}) for e in comps)
    # 超长网全程不画真线(试放与正式相位都不画)——wires_all 含已清页的试放线
    assert not any(w[2] == "X0_N1" for w in adapter.wires_all)
    # too-long 与 route-blocked 同一重落兜底:拆桩 + --offset-max 40 紧落
    tight = [c for c in adapter.calls if c[:2] == ["sch", "autoconnect"]
             and "--offset-max" in c and c[c.index("--net") + 1] == "X0_N1"]
    assert {c[c.index("--pin") + 1] for c in tight} == {"DCI0_1:1", "DCI0_2:1"}


def test_repack_pull_close_converts_long_net(tmp_path) -> None:
    """对伴拉近(P0-1/P1-6):同上 520 跨度,但组系统可用——小件 DCI0_1 被刚移
    到锚件 DCI0_2 脚旁(扫落点最优 ≈(+180,+40)),跨距落回 380 ≤ 400,网转换
    成直连线。试放与正式相位各拉近一次(落点锚定块内相对几何)。"""
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _PullFakeAdapter(
        "pass", ["dcin1", "dcin2"],
        members={"dcin1": [(0, 0, 300, 250), (650, 0, 300, 250)]})
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    evs = _audit_events(str(tmp_path))
    pulls = [e for e in evs if e.get("kind") == "pull-close"]
    assert pulls, "试放相位应发生对伴拉近"
    p0 = pulls[0]["pulls"][0]
    assert (p0["net"], p0["moved"]) == ("X0_N1", "DCI0_1")
    # 落点最优 ≈(+180,+40)(方向 -x/+y × 190;试放网格槽序随哈希种子微移,
    # snap5 上取整 → dx 180~184,dy 43±2——只锚量级,精确值无语义)
    assert 175 <= p0["dx"] <= 185 and 40 <= p0["dy"] <= 45
    # 拉近走的是单件组刚移:建组 → 重列取 gid → group-move 带同 delta
    assert any(c[:3] == ["sch", "group", "create"] for c in adapter.calls)
    mv = [c for c in adapter.calls if c[:2] == ["sch", "group-move"]]
    assert mv and float(mv[0][mv[0].index("--dx") + 1]) == p0["dx"]
    # 拉近后网转换成功(跨距 380 ≤ 门 400),直连线画出来了(试放相位,看 wires_all)
    p1 = next(e for e in evs if e.get("kind") == "compact-nets" and e.get("page") == "P1")
    assert "X0_N1" in p1["converted"]
    assert any(w[2] == "X0_N1" for w in adapter.wires_all)


def test_repack_pull_modify_fallback_rescues_clamped_move(tmp_path) -> None:
    """拉近改位回退(P0-1 虚空区救援):移动内核把纸边当硬墙拒移(rc≠0,真机
    2026-08-27 试放带全在纸外的恒态)——回退 sch modify --x/--y 改位:先拆
    mover 带网脚的桩防孤儿,再平移本体框+引脚;网照常转直连线,回退显式审计
    pull-close-fallback,主事件带 via=modify。"""
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _PullFakeAdapter(
        "pass", ["dcin1", "dcin2"],
        members={"dcin1": [(0, 0, 300, 250), (650, 0, 300, 250)]})
    adapter.group_move_rc = 1
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    evs = _audit_events(str(tmp_path))
    fb = [e for e in evs if e.get("kind") == "pull-close-fallback"]
    assert fb and fb[0]["designator"] == "DCI0_1"
    assert not [e for e in evs if e.get("kind") == "pull-close-fail"]
    # 主事件带 via=modify(几何效果与 group-move 等价)
    pc = [e for e in evs if e.get("kind") == "pull-close"]
    assert pc and pc[0]["pulls"][0]["via"] == "modify"
    # 改位调用:--id 是 primitiveId;改位前有对该脚的拆桩(先拆防孤儿;
    # 之后紧凑化轮还会再拆一次属正常顺序)
    mods = [c for c in adapter.calls if c[:2] == ["sch", "modify"] and "--x" in c]
    assert mods and mods[0][mods[0].index("--id") + 1].startswith("pc_")
    first_mod = adapter.calls.index(mods[0])
    disc = [i for i, c in enumerate(adapter.calls)
            if c[:2] == ["sch", "disconnect"] and any(a == "DCI0_1:1" for a in c)]
    assert disc and min(disc) < first_mod
    # 改位后网转换成功:跨距落回 ≤400,直连线画出来了(试放相位,看 wires_all)
    p1 = next(e for e in evs if e.get("kind") == "compact-nets" and e.get("page") == "P1")
    assert "X0_N1" in p1["converted"]
    assert any(w[2] == "X0_N1" for w in adapter.wires_all)


def test_repack_pull_both_moves_fail_keeps_span_gate(tmp_path) -> None:
    """双路皆败:group-move 拒移且 modify 改位也失败 → 记 pull-close-fail
    (不崩),线长门兜底保 netport 语义(too-long 重落收紧)。"""
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _PullFakeAdapter(
        "pass", ["dcin1", "dcin2"],
        members={"dcin1": [(0, 0, 300, 250), (650, 0, 300, 250)]})
    adapter.group_move_rc = 1
    adapter.modify_rc = 1
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    evs = _audit_events(str(tmp_path))
    assert [e for e in evs if e.get("kind") == "pull-close-fail"]
    assert not [e for e in evs if e.get("kind") == "pull-close-fallback"]
    p1 = next(e for e in evs if e.get("kind") == "compact-nets" and e.get("page") == "P1")
    assert p1["kept"]["X0_N1"].startswith("too-long(reseated")


def test_repack_list_truncation_degrades_and_warns(tmp_path) -> None:
    """sch list 1MB 截断降载(P0-2):全量变体(含 bbox)超 900KB 垃圾 → 识别
    截断特征降载重试(去 bbox),本体框退引脚凸包 ±10 仍完成转换;轮末显性化
    SCH_LIST_TRUNCATED 弱告警(此前是整页静默跳过紧凑化)。"""
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _TruncListFakeAdapter("pass", ["dcin1", "dcin2"])
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    assert adapter.bbox_attempts >= 2  # 每次读取都先试全量变体再降载
    evs = _audit_events(str(tmp_path))
    assert any(e.get("kind") == "list-degraded" and e.get("variant") == 0 for e in evs)
    # 落-量-清:compact 按实例逐块调用;只看试放相位(repack-measure 前)并集判
    m_i = next(i for i, e in enumerate(evs) if e.get("kind") == "repack-measure")
    conv = set()
    for e in evs[:m_i]:
        if e.get("kind") == "compact-nets" and e.get("page") == "P1":
            conv |= set(e.get("converted") or {})
    assert conv == {"X0_N1", "X1_N1"}  # 凸包兜底照常转换
    assert any(f.code == "SCH_LIST_TRUNCATED" and f.weak
               for f in result.rounds[0].findings)


def test_clear_failure_gates_apply_and_halts(tmp_path) -> None:
    """清页恒败门禁(P0-3):试放画布验证失败 → repack 回退流式;run 级清页
    两趟仍败 → 本轮跳过落图(0 次 gate / 0 次 block-apply),GATE_FAIL 阻塞 +
    PAGE_CLEAR_FAILED 弱告警,连败两轮 code_streak 升级 HALT——绝不在残件页
    上叠图(此前 = P1 184 件残骸 + 位号静默改号的确定源)。"""
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _ClearFailFakeAdapter("pass", ["dcin1", "dcin2"])
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "HALT"
    assert not any(c[:2] == ["sch", "gate"] for c in adapter.calls)
    assert not any(c[:2] == ["sch", "block-apply"] for c in adapter.calls)
    assert not adapter.wires
    r1 = result.rounds[0]
    assert any(f.code == "GATE_FAIL" and not f.weak for f in r1.findings)
    assert any(f.code == "PAGE_CLEAR_FAILED" and f.weak for f in r1.findings)
    evs = _audit_events(str(tmp_path))
    assert any(e.get("kind") == "repack-fallback"
               and e.get("reason") == "trial-canvas-uncleared:P1" for e in evs)
    assert any(e.get("kind") == "page-clear" and e.get("failures") for e in evs)


def test_clamp_moves_overlap_downgrade_warns(tmp_path) -> None:
    """钳移压叠降级显性化(P2-9):out-of-sheet 块的带内落点全被占(U4 占
    30-190 行右区、U2/U3 占 200-790)→ 降级「压叠最少落点」且产出
    CLAMP_OVERLAP_DOWNGRADE 弱告警(旧版无声降级,页容量不足无人知晓)。"""
    lc = _loop(FakeChat("{}"), _FakeAdapter("pass"), tmp=str(tmp_path))
    boxes = {
        "U1": {"minX": 0, "minY": 0, "maxX": 200, "maxY": 100},  # 出带(minX<30)
        "U2": {"minX": 30, "minY": 200, "maxX": 1150, "maxY": 500},
        "U3": {"minX": 30, "minY": 520, "maxX": 1150, "maxY": 790},
        "U4": {"minX": 240, "minY": 30, "maxX": 1150, "maxY": 190},
    }
    moves = lc._clamp_moves_for(
        {"type": "out-of-sheet", "a": "U1"}, boxes, {"U1": "g1"}, 30, 30, 1170, 825)
    assert moves, "降级也必须给出候选(压叠最少落点),不能空手"
    assert all(m[0] == "U1" and m[1] == "g1" for m in moves)
    assert all(30 <= m[2] and 0 <= m[3] for m in moves)  # 落点带内
    codes = [w["code"] for w in lc._layout_warnings]
    assert "CLAMP_OVERLAP_DOWNGRADE" in codes


def test_route_pin_pair_axis_and_collision() -> None:
    """路由器:轴对齐直线优先,L/Z/U 兜底;本体/他脚/他标记/共线线段四类防撞。"""
    from edaloop.loop.controller import _leg_hits_rect, _legs_collinear_overlap, _route_pin_pair

    # 异高异宽:直线只走同行/同列(平台 create 拒斜线,真机实证)→ L 兜底
    assert _route_pin_pair((290, 200), (360, 50), [], [], []) == \
        [[290, 200], [360, 200], [360, 50]]
    # 同高:直线优先
    assert _route_pin_pair((290, 200), (360, 200), [], [], []) == \
        [[290, 200], [360, 200]]
    # 同高两脚直线被本体挡住:L/Z 全部退化成同一直线,U 绕行兜底——真机
    # LED 对即此形态(R↔LED 同行,LED 另一脚+GND 地标恰在行上,直线=真
    # 短路);±40 两层绕行也被本体罩住时走到 +80
    assert _route_pin_pair((290, 200), (360, 200),
                           [(300, 100, 350, 250)], [], []) == \
        [[290, 200], [290, 280], [360, 280], [360, 200]]
    # 他网标记矩形(v0.6.11:锚点容差改矩形——netport 文字宽 60-200,锚点判据
    # 只挡文字起点,线从文字中间穿过照样压墨迹)拒直线 → U 出列绕行
    assert _route_pin_pair((290, 200), (360, 200), [], [], [(322, 188, 338, 204)]) == \
        [[290, 200], [290, 160], [360, 160], [360, 200]]
    # 与既有线段共线重叠即拒(并线短路)→ U 绕行;垂直交叉不拒;共线
    # 但不重叠不拒
    assert _route_pin_pair((290, 200), (360, 200), [], [], [],
                           [(300, 200, 400, 200)]) == \
        [[290, 200], [290, 160], [360, 160], [360, 200]]
    assert _route_pin_pair((290, 200), (360, 200), [], [], [],
                           [(0, 200, 100, 200)]) is not None
    assert _route_pin_pair((290, 200), (360, 200), [], [], [],
                           [(330, 100, 330, 190)]) is not None
    # 脚容差覆写 (x,y,tol):同行直线旁 2 单位一枚他网脚——默认容差 3 拒
    # 直线退 U;同块近旁脚覆写 1.5 放行(贴旁 2-3 单位无触点≠短路)
    assert _route_pin_pair((0, 0), (100, 0), [], [(50, 2)], []) == \
        [[0, 0], [0, -40], [100, -40], [100, 0]]
    assert _route_pin_pair((0, 0), (100, 0), [], [(50, 2, 1.5)], []) == \
        [[0, 0], [100, 0]]
    # 候选穷尽 → None:行上脚封直线/L/Z,±40..±240 全部绕行层各被一行脚占死
    # (v0.6.11 审计 P3 后 U 档位扩到 40..240,墙要封满全部绕行层)
    wall = [(325, 200), (345, 200), (325, 160), (325, 240), (325, 120), (325, 280),
            (325, 80), (325, 320), (325, 40), (325, 360), (325, 0), (325, 400),
            (325, -40), (325, 440)]
    assert _route_pin_pair((290, 200), (360, 200), [], wall, []) is None
    assert _legs_collinear_overlap((0, 0), (100, 0), (50, 0), (150, 0))
    assert not _legs_collinear_overlap((0, 0), (100, 0), (50, 5), (150, 5))
    assert not _legs_collinear_overlap((0, 0), (100, 0), (0, 50), (100, 50))
    # 本体裁定:横线穿矩形内部命中,只擦边角不命中
    assert _leg_hits_rect(0, 50, 100, 50, (10, 40, 90, 60))
    assert not _leg_hits_rect(0, 50, 100, 50, (10, 60, 90, 80))


def test_route_pin_pair_parallel_stack_and_channels() -> None:
    """路由器走线质量:近距平行堆叠拒走 + Z 角点阶梯/目的地侧 U 出通道。

    真机 run-7bb0a226ac7d C16(J3 USB-C)目检:三条 875 长横线只隔 10
    (y=5265/5275/5285)、两条长竖线只隔 5(x=830/835),缩下糊成一团;
    且引脚列被相邻脚占满的网只能贴脚行进场,源侧 U 末段必穿列——需要
    目的地侧出列的通道。"""
    from edaloop.loop.controller import _legs_parallel_stack, _route_pin_pair

    # 近距堆叠判据:平行 + 间距(2,20] + 投影重叠≥40 三者齐备才算
    assert _legs_parallel_stack((0, 0), (100, 0), (0, 10), (100, 10))
    assert not _legs_parallel_stack((0, 0), (100, 0), (0, 30), (100, 30))   # 30 间距放行
    assert not _legs_parallel_stack((0, 0), (100, 0), (80, 10), (120, 10))  # 重叠 20<40 贴脚收敛放行
    assert not _legs_parallel_stack((0, 0), (100, 0), (50, 0), (150, 0))    # 共线归并线判据管
    assert not _legs_parallel_stack((0, 0), (100, 0), (0, 10), (100, 40))   # 不平行
    # 真机 C16_N6 场景(R8.1→J3.A5):源脚行被 R9.1 挡(L1/Z1 全废)、
    # 贴脚行 y=5265 与已排腿 y=5275 近距堆叠(L2/源侧出行全废)、末段竖线
    # 被 x=1100 引脚列挡死(源侧出列全废)→ 目的地侧 -40 出列(顶通道
    # y=5225)是唯一通路
    fp = [(495, 5565), (1100, 5275), (1100, 5285), (1100, 5295)]
    assert _route_pin_pair((225, 5565), (1100, 5265), [], fp, [],
                           [(1020, 5275, 1100, 5275)]) == \
        [[225, 5565], [225, 5220], [1100, 5220], [1100, 5265]]
    # Z 角点阶梯:L1 被脚挡、L2 被脚挡、中点 Z 的竖边与已排腿共线重叠 →
    # 贴目的地 -40 的 Z 角接手(旧行为只有中点,此例无解)
    fp2 = [(280, 0), (150, 40), (300, 20)]
    assert _route_pin_pair((0, 0), (300, 40), [], fp2, [],
                           [(150, 20, 150, 60)]) == \
        [[0, 0], [260, 0], [260, 40], [300, 40]]


def test_route_pin_pair_picks_shortest_not_first_feasible() -> None:
    """最短可行(P1-4):(0,0)→(100,60) 曼哈顿族(直线/L/Z)全封死;U 源侧 -80
    (长 320)先于 U 目的地侧 +40(长 240)出现在候选序——first-feasible 拿 320,
    最短可行必须拿 240(P1 J3 长绕线根因之一:源侧远档已通就不再看近档)。"""
    from edaloop.loop.controller import _route_pin_pair

    wall = [(50, 2), (50, 58), (50, 32), (50, 22), (50, 42),
            (50, -18), (50, 82), (50, -38)]
    assert _route_pin_pair((0, 0), (100, 60), [], wall, []) == \
        [[0, 0], [0, 100], [100, 100], [100, 60]]


def test_route_pin_pair_wider_u_ladder_escapes() -> None:
    """U 档位 40..240(P1-4 扩 120/160,审计 P3 扩 200/240):±40/±80 两档全封、
    ±120 通——旧两档返回 None,引脚列密集区(真机 J3 三条 875 长横线只隔 10
    的现场)只能贴行绕远。"""
    from edaloop.loop.controller import _route_pin_pair

    wall = [(50, 2), (50, 40), (50, -40), (50, 80), (50, -80), (50, -122)]
    assert _route_pin_pair((0, 0), (100, 0), [], wall, []) == \
        [[0, 0], [0, 120], [100, 120], [100, 0]]


def test_mst_edges_beats_sorted_chain_on_star() -> None:
    """多脚网 MST(P1-5):星型拓扑(中心+四臂各 100)MST 总长 400,
    (x,y) 排序链同点集总长 600——排序序≠邻接序,链方案必绕(zig-zag)。"""
    from edaloop.loop.controller import _mst_edges

    pts = [(100, 100), (100, 0), (100, 200), (0, 100), (200, 100)]
    edges = _mst_edges(pts)
    assert len(edges) == 4
    total = sum(abs(pts[i][0] - pts[j][0]) + abs(pts[i][1] - pts[j][1]) for i, j in edges)
    assert total == 400
    chain = list(zip(sorted(pts), sorted(pts)[1:]))
    chain_total = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in chain)
    assert chain_total == 600  # 排序链基线:证明 MST 更短


def test_check_gate_oversize_page_degrades() -> None:
    """oversize 页几何族(layout-lint/clusters)fail 降弱观察交付;电气族与普通页照 blocking。"""
    rep = {
        "verdict": "fail",
        "stages": [
            {"stage": "layout-lint", "verdict": "fail", "page": "P6",
             "findings": [{"type": "out-of-sheet", "a": "DCIN1"}]},
            {"stage": "clusters", "verdict": "fail", "page": "P6",
             "findings": [{"type": "overlap", "a": "DCIN1", "b": "C10"}]},
            {"stage": "bridge-check", "verdict": "fail", "page": "P6",
             "findings": [{"type": "bridge", "net": "12V"}]},
            {"stage": "layout-lint", "verdict": "fail", "page": "P2",
             "findings": [{"type": "overlap", "a": "R1", "b": "R2"}]},
        ],
    }
    fs = check_gauge(rep, {"P6"})
    codes = [(f.code, f.weak) for f in fs]
    # P6 几何族 → 弱观察 REPLAN
    assert codes.count(("OVERSIZE_PAGE_GEOMETRY", True)) == 2
    assert all("oversize 页 P6" in f.evidence for f in fs if f.code == "OVERSIZE_PAGE_GEOMETRY")
    # P6 电气族不豁免
    assert any(f.code == "GATE_FAIL" and not f.weak and "bridge" in f.evidence for f in fs)
    # 普通页 P2 照旧强 blocking
    assert any(f.code == "GATE_FAIL" and not f.weak and "R1" in f.evidence for f in fs)


def test_repack_oversize_gets_own_page(tmp_path) -> None:
    """vehicle 型巨块(成员并集 900×1400 > 带高):oversize 审计 + 独占页左上锚。"""
    big = [(0, 0, 900, 700), (0, 750, 900, 650)]
    chat = FakeChat(json.dumps(_PLAN_2WIDE, ensure_ascii=False))
    adapter = _RepackFakeAdapter("pass", ["dcin1", "dcin2"], members={"dcin1": big})
    lc = _loop(chat, adapter, ir=_ir_with_rails(("12V", 12.0)), tmp=str(tmp_path))
    result = lc.run()
    assert result.status == "PASS"
    pack_ev = next(e for e in _audit_events(str(tmp_path)) if e.get("kind") == "repack-pack")
    assert pack_ev["oversize"] == ["dcin1"]
    assert pack_ev["pages"] == 2  # 小块 P1 + 巨块独占 P2
    # 巨块正式落图钉扎 P2
    applies = [c for c in adapter.calls if c[:2] == ["sch", "block-apply"]]
    final = applies[len(adapter.instances):]  # 试放轮之后
    assert any("--doc" in c and c[c.index("--doc") + 1] == "P2" for c in final)
