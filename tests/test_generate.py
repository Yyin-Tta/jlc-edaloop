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
    # 不传 --instance(校准 B:长 instance 名进内部网名 → netport 文字翼展 390;
    # 默认位号短名 D1_N1 翼展回落,审计走 Action.block_instance)
    assert actions[0].args[:4] == ["sch", "block-apply", "block.ams1117_ldo_3v3", "--spacing"]
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
    assert res["i0"] == ("180,300", "P1")
    assert res["i1"] == ("180,300", "P2")


def test_page_flow_stacks_small_blocks_same_page() -> None:
    """两块 AMS1117(各 dy41):300+41+60=401 接续,401+41=442 ≤ 800 同页纵排。"""
    catalog = _catalog()
    res = _at_page_of(_plan_of(catalog, "ldo-ams1117-3v3", "ldo-ams1117-3v3"), catalog)
    assert res["i0"] == ("180,300", "P1")
    assert res["i1"] == ("180,401", "P1")


def test_page_flow_big_blocks_one_per_page() -> None:
    """esp32(489)×3:849+489>800 逐块换页 → P1/P2/P3 各一块(单块仍占当前页,不静默丢)。"""
    catalog = _catalog()
    res = _at_page_of(
        _plan_of(catalog, "mcu-esp32s3-wroom1-min", "mcu-esp32s3-wroom1-min", "mcu-esp32s3-wroom1-min"),
        catalog,
    )
    assert [res[f"i{n}"] for n in range(3)] == [
        ("180,300", "P1"),
        ("180,300", "P2"),
        ("180,300", "P3"),
    ]


def test_cells_scale_with_spacing() -> None:
    """占位随 spacing 线性缩放(280 < 宽度截断点 289,不触发 clamp):dy 41→45,i1 y=405。"""
    catalog = _catalog()
    plan = _plan_of(catalog, "ldo-ams1117-3v3", "ldo-ams1117-3v3")
    actions = compile_actions(plan, catalog, spacing_default="280")
    join0 = " ".join(next(a for a in actions if a.kind == "block-apply" and a.block_instance == "i0").args)
    assert "--spacing 280" in join0
    res = {b.instance: b.at for b in plan.blocks}
    assert res["i0"] == "180,300"
    assert res["i1"] == "180,405"


def test_spacing_clamped_to_sheet_width() -> None:
    """RELAYOUT 给大 spacing 会被块宽截到 A4 内:ldo dx=856 → 上限 ⌊250*990/856⌋=289。"""
    catalog = _catalog()
    blocks = [
        {"block_id": "ldo-ams1117-3v3", "upstream_id": "block.ams1117_ldo_3v3",
         "instance": "i0", "params": {"spacing": "500"}},
    ]
    plan = BlockPlan.model_validate({"blocks": blocks})
    actions = compile_actions(plan, catalog)
    a = next(a for a in actions if a.kind == "block-apply")
    assert a.args[a.args.index("--spacing") + 1] == "289"
    # 占位按截断后格距推进:dy=int(41*289/250)=47
    res = _at_page_of(_plan_of(catalog, "ldo-ams1117-3v3", "ldo-ams1117-3v3", spacing="500"), catalog)
    assert res["i0"][0] == "180,300"
    assert res["i1"][0] == "180,407"


def test_per_block_spacing_and_at_override() -> None:
    """P4-1④:i0 params.spacing=280(截断内)→ --spacing 280 且占位放大;i1 显式 at(A4 内)优先网格。"""
    catalog = _catalog()
    blocks = [
        {"block_id": "ldo-ams1117-3v3", "upstream_id": "block.ams1117_ldo_3v3",
         "instance": "i0", "params": {"spacing": "280"}},
        {"block_id": "ldo-ams1117-3v3", "upstream_id": "block.ams1117_ldo_3v3",
         "instance": "i1", "at": "180,600"},
    ]
    plan = BlockPlan.model_validate({"blocks": blocks})
    actions = compile_actions(plan, catalog)
    by_inst = {a.block_instance: a for a in actions if a.kind == "block-apply"}
    assert by_inst["i0"].args[by_inst["i0"].args.index("--spacing") + 1] == "280"
    assert by_inst["i1"].args[by_inst["i1"].args.index("--spacing") + 1] == "250"
    res = {b.instance: (b.at, b.page) for b in plan.blocks}
    assert res["i0"] == ("180,300", "P1")  # dy 45,推进到 405
    assert res["i1"] == ("180,600", "P1")  # 显式 at 生效,页槽仍按流分配(405+41≤800 → P1)


def test_explicit_at_offsheet_falls_back() -> None:
    """RELAYOUT 给出 A4 硬界外的 at(整块飞出图纸级,run4 r2 实例 at=950,480)
    → 回退流式位,不静默落图出界(ldo dx=856:950+856=1806 ≫ 1158)。"""
    catalog = _catalog()
    plan = _plan_of(catalog, "ldo-ams1117-3v3", at="950,480")
    actions = compile_actions(plan, catalog)
    a = next(x for x in actions if x.kind == "block-apply")
    assert a.args[a.args.index("--at") + 1] == "180,300"
    # 右缘内但超可用宽(x+dx > 1158-100)同样回退:600+856=1456 > 1058
    plan2 = _plan_of(catalog, "ldo-ams1117-3v3", at="600,600")
    actions2 = compile_actions(plan2, catalog)
    a2 = next(x for x in actions2 if x.kind == "block-apply")
    assert a2.args[a2.args.index("--at") + 1] == "180,300"


def test_block_layout_anchor() -> None:
    """_BLOCK_LAYOUT 实测锚点:rs485 x0=340(U4 左翼 RS485_A/B 文字 322)、sp=210
    (三行块 dy≤498 才不顶出 A4);表内块免翼展截断,RELAYOUT 大 spacing 也不截。"""
    catalog = _catalog()
    catalog["rs485-xcvr"] = BlockRecord(
        block_id="rs485-xcvr",
        name="RS485",
        desc="x",
        category="comms",
        upstream=UpstreamRef(id="block.sp3485_rs485_halfduplex", ports={}),
    )
    plan = _plan_of(catalog, "rs485-xcvr")
    actions = compile_actions(plan, catalog)
    a = next(x for x in actions if x.kind == "block-apply")
    assert a.args[a.args.index("--at") + 1] == "340,300"
    assert a.args[a.args.index("--spacing") + 1] == "210"
    # RELAYOUT 给 500:表内免截断 → 照传(锚点几何整组实测,截断毁标定)
    plan2 = _plan_of(catalog, "rs485-xcvr", spacing="500")
    actions2 = compile_actions(plan2, catalog)
    a2 = next(x for x in actions2 if x.kind == "block-apply")
    assert a2.args[a2.args.index("--spacing") + 1] == "500"


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
    # 若误随 spacing=500 缩放(500+60),i1 应在 180,860 → 换 P2
    assert res["i0"][0] == "180,300"
    assert res["i1"][0] == "180,610"
    assert res["i1"][1] == "P1"


def test_zone_hint_overrides_category() -> None:
    """planner 显式 zone=right 优先于 category 默认(claim 变 PERI;页流下位置不变)。"""
    catalog = _catalog()
    plan = _plan_of(catalog, "ldo-ams1117-3v3", zone="right")
    actions = compile_actions(plan, catalog)
    assert next(a for a in actions if a.kind == "block-apply").zone == "PERI"
    assert plan.blocks[0].at == "180,300"
    assert plan.blocks[0].page == "P1"


# ---- P5-0/G33 行-货架页流:place 小件行内并排,宽块恒独行(与旧单列逐坐标等价) ----


def _tiny(catalog, block_id: str = "tiny", pinout: dict | None = None) -> None:
    catalog[block_id] = BlockRecord(
        block_id=block_id,
        name=block_id,
        desc="x",
        lcsc="C1",
        pinout=pinout if pinout is not None else {"1": "A", "2": "B"},
    )


def test_row_shelf_packs_small_parts_per_row(monkeypatch) -> None:
    """行-货架流(G33 页爆炸修复核心):place 小件行内左→右并排,行宽尽换行。
    实测墨迹 (150,80) → pitch = 150+2×120 = 390:行内 180/570 两位,第三件换行。"""
    from edaloop.generate import compile as compile_mod

    monkeypatch.setattr(compile_mod, "_PLACE_INK", {"tiny": (150, 80)})
    catalog = _catalog()
    _tiny(catalog)
    res = _at_page_of(_plan_of(catalog, "tiny", "tiny", "tiny"), catalog)
    assert [res[f"i{n}"] for n in range(3)] == [
        ("180,300", "P1"),
        ("570,300", "P1"),
        ("180,440", "P1"),  # 换行:行进 = 行内最大 dy(80)+gap 60
    ]


def test_row_advance_uses_max_dy_of_row(monkeypatch) -> None:
    """行进 = 行内最大 dy + gap(不是逐件 dy 累加):同行 300 高 + 80 高两件,
    下一件 y = 300+300+60 = 660。"""
    from edaloop.generate import compile as compile_mod

    monkeypatch.setattr(compile_mod, "_PLACE_INK", {"tall": (150, 300), "tiny": (150, 80)})
    catalog = _catalog()
    _tiny(catalog, "tall")
    _tiny(catalog, "tiny")
    # 带内 dy 降序:tall(300) 先,tiny(80) 拼 its 行(pitch 390+390=780 ≤ 950)
    res = _at_page_of(_plan_of(catalog, "tall", "tiny", "tiny"), catalog)
    assert res["i0"] == ("180,300", "P1")
    assert res["i1"] == ("570,300", "P1")
    assert res["i2"] == ("180,660", "P1")


def test_bands_do_not_share_rows(monkeypatch) -> None:
    """带不共行(页内行段=带,分区语义):band0 末件与 band1 首件即使都小
    也分行。"""
    from edaloop.generate import compile as compile_mod

    monkeypatch.setattr(compile_mod, "_PLACE_INK", {"tiny": (150, 80)})
    catalog = _catalog()
    _tiny(catalog)
    blocks = [
        {"block_id": "tiny", "upstream_id": "", "instance": "i0", "pins_binding": {"1": "GND"}, "zone": "left"},
        {"block_id": "tiny", "upstream_id": "", "instance": "i1", "pins_binding": {"1": "GND"}, "zone": "left"},
        {"block_id": "tiny", "upstream_id": "", "instance": "i2", "pins_binding": {"1": "GND"}, "zone": "center"},
    ]
    plan = BlockPlan.model_validate({"blocks": blocks})
    res = _at_page_of(plan, catalog)
    assert res["i0"] == ("180,300", "P1")
    assert res["i1"] == ("570,300", "P1")  # band0 行内并排
    assert res["i2"] == ("180,440", "P1")  # band1 起新行,不接 960 拼


def test_block_layout_anchor_owns_row(monkeypatch) -> None:
    """_BLOCK_LAYOUT 锚点块独占行(整组实测几何不与邻件拼行):led(dx136,
    pitch 376 本可拼行)锚定后逐块独行且 x 用实测锚点。"""
    from edaloop.generate import compile as compile_mod

    monkeypatch.setattr(compile_mod, "_BLOCK_LAYOUT", {"block.led_indicator_gpio": (200, 250)})
    catalog = _catalog()
    catalog["led"] = BlockRecord(
        block_id="led", name="L", desc="x", category="power",
        upstream=UpstreamRef(id="block.led_indicator_gpio", ports={}),
    )
    res = _at_page_of(_plan_of(catalog, "led", "led"), catalog)
    assert res["i0"] == ("200,300", "P1")  # 锚点 x0 覆盖(实测左翼已量入)
    assert res["i1"] == ("200,386", "P1")  # 行进 = dy26+60,而非拼行 x=556


def test_place_tier_defaults_by_pin_count() -> None:
    """未标定 place 器件按引脚数分档保守缺省:2 脚=旧 _CELL_PLACE(行为保真),
    3-9 脚中件,10+ 大件。"""
    from edaloop.generate.compile import _place_cell

    def rec(n: int) -> BlockRecord:
        return BlockRecord(
            block_id=f"p{n}", name="x", desc="x", lcsc="C1",
            pinout={str(i): f"P{i}" for i in range(1, n + 1)},
        )

    assert _place_cell(rec(2)) == (400, 250)
    assert _place_cell(rec(6)) == (450, 350)
    assert _place_cell(rec(12)) == (550, 500)


def test_place_ink_measured_table_wins(monkeypatch) -> None:
    """_PLACE_INK 实测墨迹优先于分档:标定过的 block_id 直接用实测格。"""
    from edaloop.generate import compile as compile_mod
    from edaloop.generate.compile import _place_cell

    monkeypatch.setattr(compile_mod, "_PLACE_INK", {"p2": (150, 80)})
    r = BlockRecord(block_id="p2", name="x", desc="x", lcsc="C1", pinout={"1": "A", "2": "B"})
    assert _place_cell(r) == (150, 80)


def test_row_shelf_12_small_parts_two_pages(monkeypatch) -> None:
    """页爆炸修复量化:req-11 类 37 小件从每件一页回到行-货架密度——
    12 件 (150,80) → 2 页收(2 件/行 × 4 行/页;旧单列流 = 3 页,
    旧未标定保守格 250 高 = 5 页)。"""
    from edaloop.generate import compile as compile_mod

    monkeypatch.setattr(compile_mod, "_PLACE_INK", {"tiny": (150, 80)})
    catalog = _catalog()
    _tiny(catalog)
    res = _at_page_of(_plan_of(catalog, *(["tiny"] * 12)), catalog)
    pages = {p for _, p in res.values()}
    assert pages == {"P1", "P2"}
