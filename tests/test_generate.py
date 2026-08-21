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
            category="power",
            upstream=_UP_LDO,
        ),
        "mcu-esp32s3-wroom1-min": BlockRecord(
            block_id="mcu-esp32s3-wroom1-min",
            name="ESP32",
            desc="mcu",
            category="mcu",
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


# ---- P4-1:功能分区布局(三带)与网格硬伤修复 ----


def _plan_of(catalog, *block_ids: str, zone: str = "") -> BlockPlan:
    blocks = []
    for n, bid in enumerate(block_ids):
        rec = catalog[bid]
        blocks.append(
            {
                "block_id": bid,
                "upstream_id": rec.upstream.id if rec.upstream else "",
                "instance": f"i{n}",
                "pins_binding": {} if rec.upstream else {"1": "GND"},
                "zone": zone,
            }
        )
    return BlockPlan.model_validate({"blocks": blocks})


def _at_of(plan: BlockPlan, catalog) -> dict[str, str]:
    compile_actions(plan, catalog)
    return {b.instance: b.at for b in plan.blocks}


def test_band_layout_separates_power_and_mcu() -> None:
    """LDO(电源带)与 MCU(主控带)锚点分列;统一槽宽 = max(2000,3200),带距 300。"""
    plan = BlockPlan.model_validate({"design_ir_id": "x", **_plan_json()})
    actions = compile_actions(plan, _catalog())
    assert plan.blocks[0].at == "400,300"
    assert plan.blocks[1].at == "3900,300"  # 400 + 槽宽 3200 + 带隙 300
    claims = {a.block_instance: a.zone for a in actions if a.kind == "block-apply"}
    assert claims["ldo1"] == "PWR" and claims["mcu1"] == "MCU"


def test_band_stack_advances_by_own_dy() -> None:
    """硬伤 A 回归:同带堆叠,下一块起点 = 前块起点 + 前块自身 dy(不再共用触发块 dy)。"""
    catalog = _catalog()
    at = _at_of(_plan_of(catalog, "ldo-ams1117-3v3", "ldo-ams1117-3v3"), catalog)
    assert at["i0"] == "400,300"
    assert at["i1"] == "400,1900"  # 300 + 自身 dy 1600


def test_band_column_wraps_at_top_limit() -> None:
    """列满换列:2800 高块堆 2 块(顶 5900),第 3 块顶到 8700 越限 → 换列 x=锚点+槽宽 3200。"""
    catalog = _catalog()
    plan = _plan_of(
        catalog,
        "mcu-esp32s3-wroom1-min", "mcu-esp32s3-wroom1-min",
        "mcu-esp32s3-wroom1-min", "mcu-esp32s3-wroom1-min",
    )
    at = _at_of(plan, catalog)
    assert at["i0"] == "3900,300"  # mcu 带 = 带1,锚点 400+3200+300
    assert at["i1"] == "3900,3100"
    assert at["i2"] == "7100,300"  # 第 3 块顶边 5900+2800=8700 > 8200 → 换列
    assert at["i3"] == "7100,3100"


def test_cells_scale_with_spacing() -> None:
    """硬伤 B 回归:spacing 1200 时占位 ×2(槽宽取 MCU 6400,LDO dy 3200)。"""
    plan = BlockPlan.model_validate({"design_ir_id": "x", **_plan_json()})
    compile_actions(plan, _catalog(), spacing_default="1200")
    assert plan.blocks[0].at == "400,300"
    assert plan.blocks[1].at == "7100,300"  # 400 + 槽宽 6400 + 带隙 300


def test_zone_hint_overrides_category() -> None:
    """planner 显式 zone=right 优先于 category 默认(claim 变 PERI,锚点占带 2 槽位)。"""
    catalog = _catalog()
    plan = _plan_of(catalog, "ldo-ams1117-3v3", zone="right")
    actions = compile_actions(plan, catalog)
    assert next(a for a in actions if a.kind == "block-apply").zone == "PERI"
    assert plan.blocks[0].at == "5000,300"  # 400 + 2*(槽宽 2000 + 带隙 300)
