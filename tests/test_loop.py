from __future__ import annotations

import json

from edaloop.generate.audit import AuditLog
from edaloop.generate.models import BlockPlan
from edaloop.intent.ir import DesignIR
from edaloop.knowledge.models import BlockRecord, RetrievedBlock, UpstreamRef
from edaloop.llm.fake import FakeChat
from edaloop.loop.attribution import attribute
from edaloop.loop.controller import LoopController
from edaloop.validate.checks import check_gauge, check_rails, check_uncovered
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
