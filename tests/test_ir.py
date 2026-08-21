from __future__ import annotations

import json

import pytest

from edaloop.intent.ir import DesignIR, Function, Power, PowerRail, Spec
from edaloop.intent.parse import IRParseError, requirement_to_ir
from edaloop.llm.fake import FakeChat


def _ir_dict() -> dict:
    return {
        "source": "test.md",
        "functions": [{"name": "锂电充电", "desc": "TP4056 1A", "constraints": ["USB-C 输入"]}],
        "interfaces": [{"type": "usb-c", "spec": "5V 供电"}],
        "power": {
            "inputs": ["USB-C 5V", "DC 12-24V"],
            "rails": [{"name": "3V3", "voltage": 3.3, "imax": 0.5}],
            "protection": "TVS+polyfuse",
        },
        "open_questions": [{"id": "Q1", "question": "升压还是降压?", "options": ["MT3608", "降压DC-DC"]}],
    }


def test_ir_roundtrip() -> None:
    ir = DesignIR.model_validate(_ir_dict())
    assert ir.power.rails[0] == PowerRail(name="3V3", voltage=3.3, imax=0.5)
    dumped = ir.model_dump_json()
    assert DesignIR.model_validate_json(dumped).id == ir.id


def test_ir_defaults() -> None:
    ir = DesignIR(source="x.md")
    assert ir.functions == [] and ir.power == Power() and ir.created is not None


def test_query_text_contains_keys() -> None:
    ir = DesignIR.model_validate(_ir_dict())
    q = ir.query_text()
    assert "锂电充电" in q and "3.3V" in q and "USB-C 5V" in q and "TVS" in q


def test_apply_answers_removes_and_bumps_revision() -> None:
    ir = DesignIR.model_validate(_ir_dict())
    assert ir.revision == 1 and len(ir.open_questions) == 1
    applied = ir.apply_answers({"Q1": "选 MT3608 升压"})
    assert applied == 1
    assert ir.open_questions == []
    assert ir.revision == 2


def test_apply_answers_partial() -> None:
    ir = DesignIR.model_validate(_ir_dict())
    applied = ir.apply_answers({"Q9": "无关答案"})
    assert applied == 0
    assert ir.revision == 1
    assert len(ir.open_questions) == 1


def test_parse_ok() -> None:
    chat = FakeChat("```json\n" + json.dumps(_ir_dict(), ensure_ascii=False) + "\n```")
    ir = requirement_to_ir("# 需求\n用 TP4056", chat)
    assert ir.functions[0].name == "锂电充电"
    assert chat.messages[0][0].role == "system"


def test_parse_bad_json() -> None:
    with pytest.raises(IRParseError):
        requirement_to_ir("# 需求", FakeChat("这不是 JSON"))


def test_parse_schema_violation() -> None:
    bad = {"source": "x", "power": {"rails": "not-a-list"}}
    with pytest.raises(IRParseError):
        requirement_to_ir("# 需求", FakeChat(json.dumps(bad)))


# ---- P4-0① IR v2:宽压轨/结构化约束/env.fab ----


def test_rail_v_range_text() -> None:
    r = PowerRail(name="VBAT", v_min=3.0, v_max=4.2, source="锂电池")
    assert r.v_text() == "3-4.2V"
    assert r.voltage is None


def test_rail_nominal_text() -> None:
    assert PowerRail(name="3V3", voltage=3.3, imax=0.5).v_text() == "3.3V"


def test_rail_no_voltage_falls_back_to_name() -> None:
    assert PowerRail(name="5V").v_text() == "5V"


def test_query_text_range_rail_no_crash() -> None:
    ir = DesignIR.model_validate(
        {"source": "t", "power": {"rails": [{"name": "VBAT", "v_min": 3.0, "v_max": 4.2}]}}
    )
    q = ir.query_text()
    assert "3-4.2V" in q and "VBAT" in q


def test_constraints_union_spec_and_str() -> None:
    f = Function(
        name="供电",
        constraints=[
            {"param": "纹波", "value": "<50", "unit": "mV", "tolerance": None},
            "低静态电流",
        ],
    )
    assert isinstance(f.constraints[0], Spec)
    assert f.constraints_digest() == "纹波=<50mV; 低静态电流"


def test_env_fab_field() -> None:
    ir = DesignIR.model_validate({"source": "t", "env": {"fab": "jlc 经济板"}})
    assert ir.env.fab == "jlc 经济板"
