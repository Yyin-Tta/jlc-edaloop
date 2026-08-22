"""P4-4③④ 单测:PARAM_OFF_SPEC 比对通道 / refine 闭环消费 / std-value 落图校验。"""

from __future__ import annotations

import json

import pytest

from edaloop.generate.models import BlockPlan
from edaloop.generate.sizing import size_for_plan
from edaloop.intent.ir import DesignIR
from edaloop.knowledge.models import BlockRecord
from edaloop.refine import collect_questions
from edaloop.validate.checks import check_param_off_spec, validate

_STD_CATALOG = {
    "resistor-std": BlockRecord(
        block_id="resistor-std", name="标准电阻", desc="params.value 查标准件表",
        category="passive", tags=["std-value", "resistor"], ports=["1", "2"], pinout={"1": "1", "2": "2"},
    ),
    "capacitor-std": BlockRecord(
        block_id="capacitor-std", name="标准电容", desc="params.value 查标准件表",
        category="passive", tags=["std-value", "capacitor"], ports=["1", "2"], pinout={"1": "1", "2": "2"},
    ),
}


def _led_ir() -> DesignIR:
    return DesignIR.model_validate(
        {"source": "t.md", "power": {"rails": [{"name": "3V3", "voltage": 3.3}]}}
    )


def _buck_ir() -> DesignIR:
    return DesignIR.model_validate(
        {"source": "t.md", "power": {"rails": [
            {"name": "5V", "voltage": 5.0},
            {"name": "3V3", "voltage": 3.3, "imax": 1.0},
        ]}}
    )


def _led_plan(r_value: str, *, drive_net: str = "NET_LED1") -> BlockPlan:
    return BlockPlan.model_validate({"blocks": [
        {"block_id": "led-indicator", "instance": "led1",
         "ports_binding": {"DRV": drive_net, "GND": "GND"}},
        {"block_id": "resistor-std", "instance": "r1",
         "pins_binding": {"1": drive_net, "2": "NET_LED_A"},
         "params": {"value": r_value}},
    ]})


def _buck_plan(cap_specs: list[tuple[str, str, str]]) -> BlockPlan:
    """cap_specs: (instance, value, '3V3,GND' 网对) 的 capacitor-std 集。"""
    caps = [
        {"block_id": "capacitor-std", "instance": inst,
         "pins_binding": {"1": nets.split(",")[0], "2": nets.split(",")[1]},
         "params": {"value": val}}
        for inst, val, nets in cap_specs
    ]
    return BlockPlan.model_validate({"blocks": [
        {"block_id": "up-sy8089_buck_3v3", "instance": "b1",
         "ports_binding": {"VIN": "5V", "3V3": "3V3", "GND": "GND"}},
        *caps,
    ]})


# ---- 通道 A:LED 建议目标直连(std 电阻 instance == 建议所属 LED 块) ----


def test_channel_a_wrong_led_resistor_flagged() -> None:
    ir = _led_ir()
    advices = size_for_plan([{"block_id": "led-indicator", "instance": "r1",
                              "ports_binding": {"DRV": "3V3", "GND": "GND"}}], ir=ir, catalog=_STD_CATALOG)
    plan = _led_plan("10k")
    findings = check_param_off_spec(plan, advices, catalog=_STD_CATALOG)
    assert any(f.code == "PARAM_OFF_SPEC" for f in findings)
    f = next(f for f in findings if f.code == "PARAM_OFF_SPEC")
    assert f.weak and f.severity == "warn"
    assert "10k" in f.evidence and ("270" in f.evidence or "260" in f.evidence)


def test_channel_a_e24_adjacent_not_flagged() -> None:
    ir = _led_ir()
    advices = size_for_plan([{"block_id": "led-indicator", "instance": "r1",
                              "ports_binding": {"DRV": "3V3", "GND": "GND"}}], ir=ir, catalog=_STD_CATALOG)
    rec = next(a for a in advices if a.rec_kind == "resistance").rec_value
    # 表内邻档值(330 vs 270,ratio 1.22 < 1.35)在容差内
    adjacent = "330" if rec == "270" else rec
    assert check_param_off_spec(_led_plan(adjacent), advices, catalog=_STD_CATALOG) == []


# ---- 通道 B:LED 驱动网相交(std 电阻不必同名于建议目标) ----


def test_channel_b_led_net_intersect_flagged() -> None:
    """GPIO 驱动 LED(单轨推断):串联限流 R 与驱动网恰一网相交 → 判。"""
    ir = _led_ir()  # 单 3V3 轨 → 驱动网 NET_LED1 单轨推断 3.3V
    advices = size_for_plan(_led_plan("10k").blocks[:1], ir=ir, catalog=_STD_CATALOG)
    findings = check_param_off_spec(_led_plan("47k"), advices, catalog=_STD_CATALOG)
    assert any(f.code == "PARAM_OFF_SPEC" for f in findings)
    led = next(a for a in advices if a.kind == "led-resistor")
    assert led.nets == ["NET_LED1"]
    assert "单轨推断" in led.inputs[0][2]


def test_channel_b_led_rail_drive_skipped() -> None:
    """轨直挂 LED(电源指示灯):驱动网=轨,该节点外部 R 拓扑不可分(上拉/限流)→ 不判。"""
    ir = _led_ir()
    led_rail = _led_plan("10k", drive_net="3V3")
    advices = size_for_plan(led_rail.blocks[:1], ir=ir, catalog=_STD_CATALOG)
    assert next(a for a in advices if a.kind == "led-resistor").nets == ["3V3"]
    assert check_param_off_spec(led_rail, advices, catalog=_STD_CATALOG) == []


# ---- 通道 B:buck 输出电容网组(共存容差成员豁免) ----


def test_channel_b_buck_cout_wrong_value_flagged() -> None:
    ir = _buck_ir()
    plan = _buck_plan([("c1", "100n", "3V3,GND")])
    advices = size_for_plan(plan.blocks[:1], ir=ir, catalog=_STD_CATALOG)
    cout = next(a for a in advices if a.rec_kind == "capacitance")
    assert cout.nets == ["3V3", "GND"]
    findings = check_param_off_spec(plan, advices, catalog=_STD_CATALOG)
    assert any(f.code == "PARAM_OFF_SPEC" for f in findings)


def test_channel_b_buck_cout_sibling_in_tolerance_suppresses() -> None:
    """100n 去耦 + 22µF 纹波电容共存于同一网对是正常设计:组内有合格成员不判罪。"""
    ir = _buck_ir()
    plan = _buck_plan([("c1", "100n", "3V3,GND"), ("c2", "22u", "3V3,GND")])
    advices = size_for_plan(plan.blocks[:1], ir=ir, catalog=_STD_CATALOG)
    assert check_param_off_spec(plan, advices, catalog=_STD_CATALOG) == []


def test_missing_part_not_flagged() -> None:
    """缺件(建议有、图上无)不报——块内可能已含该元件,归 critic/refine 问答。"""
    ir = _led_ir()
    plan = BlockPlan.model_validate({"blocks": [
        {"block_id": "led-indicator", "instance": "led1",
         "ports_binding": {"DRV": "3V3", "GND": "GND"}},
    ]})
    advices = size_for_plan(plan.blocks, ir=ir, catalog=_STD_CATALOG)
    assert any(a.rec_kind == "resistance" for a in advices)
    assert check_param_off_spec(plan, advices, catalog=_STD_CATALOG) == []


# ---- validate() 入口贯通(sizing 非空才启用) ----


def test_validate_entry_wires_sizing() -> None:
    ir = _led_ir()
    plan = _led_plan("10k")
    advices = size_for_plan(plan.blocks, ir=ir, catalog=_STD_CATALOG)
    findings = validate(ir, plan, {"rails_ok": True, "pass": True, "checks": []},
                        catalog=_STD_CATALOG, sizing=advices)
    assert any(f.code == "PARAM_OFF_SPEC" and f.weak for f in findings)


def test_validate_without_sizing_skips_check() -> None:
    ir = _led_ir()
    plan = _led_plan("10k")
    findings = validate(ir, plan, {"rails_ok": True, "pass": True, "checks": []},
                        catalog=_STD_CATALOG)
    assert not any(f.code == "PARAM_OFF_SPEC" for f in findings)


# ---- ④ refine 闭环:critic / round-validate 审计事件 → 问题队列 ----


def test_collect_questions_critic_and_param_events(tmp_path) -> None:
    events = [
        {"kind": "ir", "ir": {"source": "t.md", "open_questions": []}},
        {"kind": "critic", "findings": [
            {"code": "CRITIC_DECOUPLING", "evidence": "u1 缺 100nF 去耦电容"},
        ]},
        {"kind": "round-validate", "round_no": 2, "weak_codes": ["PARAM_OFF_SPEC"],
         "weak": ["选值 [r1=10k] vs sizing 建议 270(led-resistor@led1)"]},
    ]
    d = tmp_path / "run-x"
    d.mkdir()
    (d / "audit.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")
    qs = collect_questions(str(d))
    by_src = {q["source"]: q for q in qs}
    assert "critic" in by_src and "param-off-spec" in by_src
    cq = by_src["critic"]
    assert cq["options"] == ["采纳补件(按建议重规划)", "人工忽略"]
    assert "去耦" in cq["question"]
    pq = by_src["param-off-spec"]
    assert pq["options"] == ["采纳建议值(补/换标准件重规划)", "人工忽略"]
    assert "10k" in pq["question"]


# ---- ② make_plan 对 std-value 块的校验(值必须表内 + 引脚 1/2) ----


def _std_candidates():
    from edaloop.knowledge.models import RetrievedBlock
    return [RetrievedBlock(
        block_id="resistor-std", name="标准电阻", desc="params.value 查标准件表",
        category="passive", tags=["std-value", "resistor"], parts=[],
        ports=["1", "2"], provenance="P4-4②", upstream=None,
        score=1.0, channels=["std"], rank=1, pinout={"1": "1", "2": "2"},
    )]


def _std_plan_json(value: str, pins: dict | None = None) -> dict:
    return {
        "blocks": [{
            "block_id": "resistor-std", "instance": "r1",
            "pins_binding": pins or {"1": "3V3", "2": "NET_LED_A"},
            "params": {"value": value},
            "provenance": "sizing 反馈 led 限流 270Ω",
        }],
        "nets": [{"name": "3V3", "class": "power"}, {"name": "GND", "class": "power"}],
        "confidence": 0.9,
        "provenance": ["std"],
    }


def test_make_plan_accepts_ontable_std_value() -> None:
    from edaloop.generate.plan import make_plan
    from edaloop.llm.fake import FakeChat
    chat = FakeChat("```json\n" + json.dumps(_std_plan_json("330"), ensure_ascii=False) + "\n```")
    plan = make_plan(_led_ir(), _std_candidates(), chat)
    assert plan.blocks[0].params["value"] == "330"


def test_make_plan_rejects_offtable_std_value() -> None:
    from edaloop.generate.plan import PlanError, make_plan
    from edaloop.llm.fake import FakeChat
    chat = FakeChat(json.dumps(_std_plan_json("331"), ensure_ascii=False))
    with pytest.raises(PlanError) as ei:
        make_plan(_led_ir(), _std_candidates(), chat)
    assert "标准件表" in str(ei.value)


def test_make_plan_rejects_std_bad_pin_number() -> None:
    from edaloop.generate.plan import PlanError, make_plan
    from edaloop.llm.fake import FakeChat
    chat = FakeChat(json.dumps(_std_plan_json("330", pins={"3": "3V3", "2": "GND"}), ensure_ascii=False))
    with pytest.raises(PlanError) as ei:
        make_plan(_led_ir(), _std_candidates(), chat)
    assert "引脚" in str(ei.value)


def test_make_plan_retry_carries_rejection_reason() -> None:
    """P4-4②残留修复:校验拒绝后重问要带原因,attempts 内收敛(req-11 目录外块耗尽重试 ERROR 实证)。"""
    from edaloop.generate.plan import make_plan
    from edaloop.llm.fake import FakeChat
    bad = {"blocks": [{"block_id": "made-up-block", "instance": "x1",
                       "pins_binding": {"1": "3V3", "2": "GND"}, "provenance": "幻觉"}],
           "nets": [], "confidence": 0.5, "provenance": []}
    chat = FakeChat([json.dumps(bad, ensure_ascii=False),
                     json.dumps(_std_plan_json("330"), ensure_ascii=False)])
    plan = make_plan(_led_ir(), _std_candidates(), chat)
    assert plan.blocks[0].params["value"] == "330"
    assert len(chat.messages) == 2
    assert "目录外" in chat.messages[1][1].content  # 第二次请求带上拒绝原因


def test_ensure_std_candidates_backfills_channel() -> None:
    """P4-4②残留修复:检索没召回 std 块时从全量 catalog 补,通道恒可用;幂等;catalog 缺则原样。"""
    from edaloop.generate.plan import ensure_std_candidates
    from edaloop.knowledge.models import RetrievedBlock
    other = RetrievedBlock(block_id="led-indicator", name="LED", desc="", category="indicator",
                           tags=[], parts=[], ports=["DRV", "GND"], provenance="t",
                           score=1.0, channels=["kw"], rank=1)
    cat = dict(_STD_CATALOG)
    merged = ensure_std_candidates([other], cat)
    ids = [c.block_id for c in merged]
    assert "resistor-std" in ids and "capacitor-std" in ids and "led-indicator" in ids
    assert len(ensure_std_candidates(merged, cat)) == len(merged)  # 幂等
    assert ensure_std_candidates([other], None) == [other]  # 无 catalog 原样返回


# ---- ② compile std-value 通道:值查表得 lcsc(落图动作可执行) ----


def test_compile_std_value_resolves_lcsc_and_rejects_miss() -> None:
    from edaloop.generate.compile import CompileError, _fill_bindings, compile_actions
    from edaloop.generate.stdparts import load_table

    # _fill_bindings:表内值过,表外值硬错
    ok = BlockPlan.model_validate({"blocks": [{
        "block_id": "resistor-std", "instance": "r1",
        "pins_binding": {"1": "3V3", "2": "NET_LED_A"}, "params": {"value": "330"},
    }]})
    _fill_bindings(ok, _STD_CATALOG)
    miss = BlockPlan.model_validate({"blocks": [{
        "block_id": "resistor-std", "instance": "r1",
        "pins_binding": {"1": "3V3", "2": "NET_LED_A"}, "params": {"value": "331"},
    }]})
    with pytest.raises(CompileError) as ei:
        _fill_bindings(miss, _STD_CATALOG)
    assert "不在标准件表" in str(ei.value)

    # compile_actions:lib-search 动作带上表内 C 号
    plan = BlockPlan.model_validate({"blocks": [
        {"block_id": "resistor-std", "instance": "r1",
         "pins_binding": {"1": "3V3", "2": "NET_LED_A"},
         "params": {"value": "330", "x": "500", "y": "500"}},
    ]})
    actions = compile_actions(plan, _STD_CATALOG)
    search = next(a for a in actions if a.kind == "lib-search")
    want_c = load_table()["resistor"]["330"]["lcsc"]
    assert search.lcsc == want_c
    assert want_c in search.args
