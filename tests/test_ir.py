from __future__ import annotations

import json

import pytest

from edaloop.intent.ir import DesignIR, Power, PowerRail
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
