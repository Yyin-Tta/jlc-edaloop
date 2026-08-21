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


# ---- P4-b2:A4 页流布局(1170×825,实测墨迹表 @250)+ per-block 覆盖 ----
# 实测占位:ams1117 dy=41,esp32s3 dy=489(spacing 250 标定);y0=300,top=800,间隙 60。


def _plan_of(catalog, *block_ids: str, zone: str = "", at: str = "", spacing: str = "") -> BlockPlan:
    blocks = []
    for n, bid in enumerate(block_ids):
        rec = catalog[bid]
        params = {"spacing": spacing} if spacing else {}
        blocks.append(
            {
                "block_id": bid,
                "upstream_id": rec.upstream.id if rec.upstream else "",
                "instance": f"i{n}",
                "pins_binding": {} if rec.upstream else {"1": "GND"},
                "params": params,
                "zone": zone,
                "at": at,
            }
        )
    return BlockPlan.model_validate({"blocks": blocks})


def _at_page_of(plan: BlockPlan, catalog, **kw) -> dict[str, tuple[str, str]]:
    compile_actions(plan, catalog, **kw)
    return {b.instance: (b.at, b.page) for b in plan.blocks}


def test_page_flow_band_order_and_wrap() -> None:
    """带序入页:LDO(带0,dy41)先入 P1;MCU(带1,dy489)接续 401+489>800 → 换 P2 页首。"""
    catalog = _catalog()
    res = _at_page_of(_plan_of(catalog, "ldo-ams1117-3v3", "mcu-esp32s3-wroom1-min"), catalog)
    assert res["i0"] == ("100,300", "P1")
    assert res["i1"] == ("100,300", "P2")


def test_page_flow_stacks_small_blocks_same_page() -> None:
    """两块 AMS1117(各 dy41):300+41+60=401 接续,401+41=442 ≤ 800 同页纵排。"""
    catalog = _catalog()
    res = _at_page_of(_plan_of(catalog, "ldo-ams1117-3v3", "ldo-ams1117-3v3"), catalog)
    assert res["i0"] == ("100,300", "P1")
    assert res["i1"] == ("100,401", "P1")


def test_page_flow_big_blocks_one_per_page() -> None:
    """esp32(489)×3:849+489>800 逐块换页 → P1/P2/P3 各一块(单块仍占当前页,不静默丢)。"""
    catalog = _catalog()
    res = _at_page_of(
        _plan_of(catalog, "mcu-esp32s3-wroom1-min", "mcu-esp32s3-wroom1-min", "mcu-esp32s3-wroom1-min"),
        catalog,
    )
    assert [res[f"i{n}"] for n in range(3)] == [
        ("100,300", "P1"),
        ("100,300", "P2"),
        ("100,300", "P3"),
    ]


def test_cells_scale_with_spacing() -> None:
    """占位随 spacing 线性缩放(300 < 宽度截断点,不触发 clamp):dy 41→49,i1 y=300+49+60=409。"""
    catalog = _catalog()
    plan = _plan_of(catalog, "ldo-ams1117-3v3", "ldo-ams1117-3v3")
    actions = compile_actions(plan, catalog, spacing_default="300")
    join0 = " ".join(next(a for a in actions if a.kind == "block-apply" and a.block_instance == "i0").args)
    assert "--spacing 300" in join0
    res = {b.instance: b.at for b in plan.blocks}
    assert res["i0"] == "100,300"
    assert res["i1"] == "100,409"


def test_spacing_clamped_to_sheet_width() -> None:
    """RELAYOUT 给大 spacing 会被块宽截到 A4 内:ldo dx=856 → 上限 ⌊250*1070/856⌋=312。"""
    catalog = _catalog()
    blocks = [
        {"block_id": "ldo-ams1117-3v3", "upstream_id": "block.ams1117_ldo_3v3",
         "instance": "i0", "params": {"spacing": "500"}},
    ]
    plan = BlockPlan.model_validate({"blocks": blocks})
    actions = compile_actions(plan, catalog)
    a = next(a for a in actions if a.kind == "block-apply")
    assert a.args[a.args.index("--spacing") + 1] == "312"
    # 占位按截断后格距推进:dy=int(41*312/250)=51
    res = _at_page_of(_plan_of(catalog, "ldo-ams1117-3v3", "ldo-ams1117-3v3", spacing="500"), catalog)
    assert res["i0"][0] == "100,300"
    assert res["i1"][0] == "100,411"


def test_per_block_spacing_and_at_override() -> None:
    """P4-1④:i0 params.spacing=300(截断内)→ --spacing 300 且占位放大;i1 显式 at 优先网格。"""
    catalog = _catalog()
    blocks = [
        {"block_id": "ldo-ams1117-3v3", "upstream_id": "block.ams1117_ldo_3v3",
         "instance": "i0", "params": {"spacing": "300"}},
        {"block_id": "ldo-ams1117-3v3", "upstream_id": "block.ams1117_ldo_3v3",
         "instance": "i1", "at": "600,600"},
    ]
    plan = BlockPlan.model_validate({"blocks": blocks})
    actions = compile_actions(plan, catalog)
    by_inst = {a.block_instance: a for a in actions if a.kind == "block-apply"}
    assert by_inst["i0"].args[by_inst["i0"].args.index("--spacing") + 1] == "300"
    assert by_inst["i1"].args[by_inst["i1"].args.index("--spacing") + 1] == "250"
    res = {b.instance: (b.at, b.page) for b in plan.blocks}
    assert res["i0"] == ("100,300", "P1")  # dy 49,推进到 409
    assert res["i1"] == ("600,600", "P1")  # 显式 at 生效,页槽仍按流分配(409+41≤800 → P1)


def test_emission_page_consecutive_flow_order() -> None:
    """产出序=流序(页连续升序)而非 plan 原序:plan 先 mcu 后 ldo,产出 ldo(P1) 在 mcu(P2) 前。

    --doc 切换粘性,跨页交错产出会让前台来回摆 + P1 动作夹在 P2+ 之后落错页。
    """
    catalog = _catalog()
    plan = _plan_of(catalog, "mcu-esp32s3-wroom1-min", "ldo-ams1117-3v3")
    actions = compile_actions(plan, catalog)
    applies = [a for a in actions if a.kind == "block-apply"]
    assert [a.block_instance for a in applies] == ["i1", "i0"]  # ldo(band0) 先于 mcu(band1)
    assert [a.page for a in applies] == ["P1", "P2"]


def test_place_cell_not_scaled_by_spacing() -> None:
    """place 通道符号几何与 spacing 无关(sch place 无该旗标):格恒 400×250,不随 params 缩。"""
    catalog = _catalog()
    catalog["part-res-pull"] = BlockRecord(
        block_id="part-res-pull", name="R", desc="x", lcsc="C22878",
        pinout={"1": "A", "2": "B"},
    )
    # zone=left 全归电源带,大块先放:place(250) → ldo(51) → ldo(51)
    plan = _plan_of(catalog, "part-res-pull", "ldo-ams1117-3v3", "ldo-ams1117-3v3", zone="left", spacing="500")
    compile_actions(plan, catalog)
    res = {b.instance: (b.at, b.page) for b in plan.blocks}
    # i0 place 占位恒 250(gap 60):i1 ldo 在 y=300+250+60=610 同页;
    # 若误随 spacing=500 缩放(500+60),i1 应在 100,860 → 换 P2
    assert res["i0"][0] == "100,300"
    assert res["i1"][0] == "100,610"
    assert res["i1"][1] == "P1"


def test_zone_hint_overrides_category() -> None:
    """planner 显式 zone=right 优先于 category 默认(claim 变 PERI;页流下位置不变)。"""
    catalog = _catalog()
    plan = _plan_of(catalog, "ldo-ams1117-3v3", zone="right")
    actions = compile_actions(plan, catalog)
    assert next(a for a in actions if a.kind == "block-apply").zone == "PERI"
    assert plan.blocks[0].at == "100,300"
    assert plan.blocks[0].page == "P1"
