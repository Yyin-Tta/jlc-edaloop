from __future__ import annotations

import json

import pytest

from edaloop.generate.compile import CompileError, compile_actions
from edaloop.generate.models import BlockPlan
from edaloop.generate.plan import PlanError, make_plan
from edaloop.intent.ir import DesignIR
from edaloop.knowledge.models import BlockRecord, RetrievedBlock, UpstreamRef
from edaloop.llm.fake import FakeChat

_UP_LDO = UpstreamRef(id="block.ams1117_ldo_3v3", ports={"VIN_5V": "+5V", "3V3": "+3V3", "GND": "GND"})
_UP_MCU = UpstreamRef(
    id="block.esp32s3_wroom1_module",
    ports={"3V3": "3V3", "GND": "GND", "EN": "EN", "IO0": "IO0", "U0TXD": "MCU_TX", "U0RXD": "MCU_RX"},
)


def _candidates() -> list[RetrievedBlock]:
    return [
        RetrievedBlock(
            block_id="ldo-ams1117-3v3",
            name="AMS1117-3.3 LDO",
            desc="5V 转 3.3V",
            category="power",
            tags=["ldo"],
            parts=[],
            ports=[],
            provenance="",
            upstream=_UP_LDO,
            score=0.9,
            channels=["dense"],
            rank=1,
        ),
        RetrievedBlock(
            block_id="mcu-esp32s3-wroom1-min",
            name="ESP32-S3-WROOM-1",
            desc="模组最小系统",
            category="mcu",
            tags=["esp32"],
            parts=[],
            ports=[],
            provenance="",
            upstream=_UP_MCU,
            score=0.85,
            channels=["dense"],
            rank=2,
        ),
    ]


def _catalog() -> dict[str, BlockRecord]:
    return {
        "ldo-ams1117-3v3": BlockRecord(
            block_id="ldo-ams1117-3v3",
            name="AMS1117",
            desc="ldo",
            upstream=_UP_LDO,
        ),
        "mcu-esp32s3-wroom1-min": BlockRecord(
            block_id="mcu-esp32s3-wroom1-min",
            name="ESP32",
            desc="mcu",
            upstream=_UP_MCU,
        ),
    }


def _ir() -> DesignIR:
    return DesignIR.model_validate(
        {
            "source": "req.md",
            "functions": [{"name": "mcu 最小系统"}],
            "power": {"rails": [{"name": "3V3", "voltage": 3.3}]},
        }
    )


def _plan_json() -> dict:
    return {
        "blocks": [
            {
                "block_id": "ldo-ams1117-3v3",
                "upstream_id": "block.ams1117_ldo_3v3",
                "instance": "ldo1",
                "ports_binding": {"VIN_5V": "5V", "3V3": "3V3", "GND": "GND"},
                "provenance": "0.9",
            },
            {
                "block_id": "mcu-esp32s3-wroom1-min",
                "upstream_id": "block.esp32s3_wroom1_module",
                "instance": "mcu1",
                "ports_binding": {"3V3": "3V3", "GND": "GND", "EN": "EN", "IO0": "IO0", "U0TXD": "MCU_TX", "U0RXD": "MCU_RX"},
                "provenance": "0.85",
            },
        ],
        "nets": [{"name": "3V3", "class": "power"}, {"name": "GND", "class": "power"}],
        "confidence": 0.9,
        "provenance": ["最小系统"],
    }


def test_make_plan_ok() -> None:
    chat = FakeChat("```json\n" + json.dumps(_plan_json(), ensure_ascii=False) + "\n```")
    plan = make_plan(_ir(), _candidates(), chat)
    assert plan.blocks[0].instance == "ldo1"
    assert plan.design_ir_id == _ir().id or plan.design_ir_id


def test_make_plan_rejects_unknown_block() -> None:
    bad = _plan_json()
    bad["blocks"][0]["block_id"] = "ghost-block"
    chat = FakeChat(json.dumps(bad, ensure_ascii=False))
    with pytest.raises(PlanError):
        make_plan(_ir(), _candidates(), chat)


def test_compile_actions_binds_all_ports() -> None:
    plan = BlockPlan.model_validate({"design_ir_id": "x", **_plan_json()})
    actions = compile_actions(plan, _catalog())
    assert actions[0].args[:4] == ["sch", "block-apply", "block.ams1117_ldo_3v3", "--instance"]
    joined = " ".join(actions[0].args)
    assert "--bind VIN_5V=5V" in joined and "--bind 3V3=3V3" in joined and "--json" in joined
    assert actions[-1].kind == "sch-gate"


def test_compile_fills_missing_ports_with_defaults() -> None:
    data = _plan_json()
    data["blocks"][1]["ports_binding"] = {"3V3": "3V3"}
    plan = BlockPlan.model_validate(data)
    actions = compile_actions(plan, _catalog())
    joined = " ".join(actions[1].args)
    assert "U0TXD=MCU_TX" in joined and "GND=GND" in joined


def test_compile_rejects_bad_port() -> None:
    data = _plan_json()
    data["blocks"][0]["ports_binding"] = {"NOPE": "5V"}
    plan = BlockPlan.model_validate(data)
    with pytest.raises(CompileError):
        compile_actions(plan, _catalog())


def test_compile_rejects_upstream_mismatch() -> None:
    data = _plan_json()
    data["blocks"][0]["upstream_id"] = "block.wrong"
    plan = BlockPlan.model_validate(data)
    with pytest.raises(CompileError):
        compile_actions(plan, _catalog())


def test_compile_rejects_no_upstream() -> None:
    data = _plan_json()
    data["blocks"][0]["block_id"] = "mcu-stm32f103c8-min"
    data["blocks"][0]["upstream_id"] = ""
    catalog = _catalog()
    catalog["mcu-stm32f103c8-min"] = BlockRecord(block_id="mcu-stm32f103c8-min", name="stm32", desc="x")
    plan = BlockPlan.model_validate(data)
    with pytest.raises(CompileError):
        compile_actions(plan, catalog)
