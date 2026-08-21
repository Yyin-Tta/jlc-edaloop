from __future__ import annotations

import json

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

    def run_json(self, args):
        self.calls.append(args)
        if args[1] == "gate":
            return {"verdict": self.gate_verdict, "stages": []}
        if args[1] == "pages":
            return {"result": {"pages": [{"name": "P1", "uuid": "u1", "parentSchematicUuid": "s1"}]}}
        if args[1] == "page-new":
            return {"result": {"pageUuid": f"pg-{len(self.calls)}"}}
        if args[1] == "block-apply":
            return {"ok": "applied", "placed": [{"designator": "U1"}, {"designator": "C1"}]}
        return {"ok": "applied"}


def _zones_calls(adapter) -> dict[str, list]:
    out: dict[str, list] = {}
    for c in adapter.calls:
        if c and c[0] == "sch" and c[1] in ("zones", "zone-draw", "zone-plan", "note"):
            out.setdefault(c[1], []).append(c)
    return out


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
    #    通用路径 run+run_json 双记录,相邻去重
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
