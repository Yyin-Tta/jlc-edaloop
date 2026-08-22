"""P4-4① 输入来源表断言(Go:轨相关硬编码清零断言可执行)。

口径:
- 轨电压必须出自 IR rail / 网名家族兜底 / IR 单轨推断 —— 非常规轨(2.8V)能原样流入公式,
  证明代码路径上没有 3.3/5/12 字面量兜底;
- IR 声明优先于家族兜底(同名网 IR 说 5.2V 就用 5.2V);
- 真实建议(result_rec != n/a)的每个输入都有出处;轨输入缺出处 = 硬编码嫌疑,gap() 必须为 None;
- Vf/If/f_sw 走器件参数槽:槽有值时出处标「参数槽」,缺省走 ENGINEERING-DEFAULT 具名缺省。
"""

from __future__ import annotations

from edaloop.generate.sizing import size_for_plan
from edaloop.intent.ir import DesignIR
from edaloop.knowledge.models import BlockRecord, Electrical

_ALLOWED_RAIL_SRC = ("IR rail", "网名家族兜底", "IR 单轨推断")


def _ir(rails: list[dict]) -> DesignIR:
    return DesignIR.model_validate({"source": "t.md", "power": {"rails": rails}})


def _led(net: str = "NET_A") -> dict:
    return {"block_id": "led-indicator", "instance": "led1", "ports_binding": {"DRV": net, "GND": "GND"}}


def test_unusual_rail_flows_through() -> None:
    """2.8V 非常规轨:R=(2.8-2.0)/5mA=160Ω —— 若代码里有 3.3 字面量,这里必然算错。"""
    advs = size_for_plan([_led("VDD_IO")], ir=_ir([{"name": "VDD_IO", "voltage": 2.8}]))
    a = next(x for x in advs if x.kind == "led-resistor")
    assert abs(a.result_raw - 160.0) < 0.5
    rail = next(i for i in a.inputs if i[0] == "V_rail")
    assert rail[2].startswith("IR rail") and "2.8" in rail[2]


def test_ir_rail_beats_family_fallback() -> None:
    """IR 声明 5.2V 的 '5V' 网:优先于网名家族 5.0V。"""
    advs = size_for_plan([_led("5V")], ir=_ir([{"name": "5V", "voltage": 5.2}]))
    a = next(x for x in advs if x.kind == "led-resistor")
    rail = next(i for i in a.inputs if i[0] == "V_rail")
    assert "5.2" in rail[2] and rail[2].startswith("IR rail")


def test_rail_inputs_always_have_allowed_source() -> None:
    """所有真实建议的轨输入出处 ∈ {IR rail, 家族兜底, 单轨推断};缺出处即失败。"""
    ir = _ir([
        {"name": "3V3", "voltage": 3.3, "imax": 1.0},
        {"name": "VBAT", "v_min": 3.0, "v_max": 4.2, "imax": 2.0},
        {"name": "5V", "voltage": 5.0},
    ])
    blocks = [
        _led("3V3"),
        {"block_id": "ldo-ams1117-3v3", "instance": "ldo1",
         "ports_binding": {"VIN_5V": "5V", "3V3": "3V3", "GND": "GND"}},
        {"block_id": "boost-mt3608", "instance": "bo1",
         "ports_binding": {"VIN": "VBAT", "VOUT": "5V", "GND": "GND"}},
    ]
    advs = size_for_plan(blocks, ir=ir)
    real = [a for a in advs if a.result_rec != "n/a"]
    assert real
    for a in real:
        assert a.gap() is None, f"{a.kind}@{a.target}: {a.gap()}"
        for name, _val, src in a.inputs:
            if name in ("V_rail", "V_in", "V_out", "V_trip", "I_load"):
                assert any(src.startswith(p) for p in _ALLOWED_RAIL_SRC), f"{a.kind}@{a.target} {name}: {src}"


def test_no_ir_no_guess() -> None:
    """无 IR + 家族兜底不出的网:轨相关公式降级(n/a + ⚠ 记号),不猜数值。"""
    advs = size_for_plan([_led("NET_A")])
    a = next(x for x in advs if x.kind == "led-resistor")
    assert a.result_rec == "n/a"
    assert a.gap() is not None  # 轨输入显式标缺,不静默


def test_device_param_slots_beat_defaults() -> None:
    """vf/if 槽有值 → 出处「参数槽」;缺省走 ENGINEERING-DEFAULT 具名缺省。"""
    cat = {
        "led-indicator": BlockRecord(
            block_id="led-indicator", name="led", desc="",
            electrical=Electrical(params={"vf": "1.8", "if": "2"}, source="test"),
        )
    }
    advs = size_for_plan([_led("3V3")], ir=_ir([{"name": "3V3", "voltage": 3.3}]), catalog=cat)
    a = next(x for x in advs if x.kind == "led-resistor")
    srcs = {i[0]: i[2] for i in a.inputs}
    assert "参数槽 vf=1.8" in srcs["V_f"] and "参数槽 if=2" in srcs["I_f"]
    assert abs(a.result_raw - (3.3 - 1.8) / 0.002) < 1

    advs2 = size_for_plan([_led("3V3")], ir=_ir([{"name": "3V3", "voltage": 3.3}]))
    a2 = next(x for x in advs2 if x.kind == "led-resistor")
    srcs2 = {i[0]: i[2] for i in a2.inputs}
    assert srcs2["V_f"].startswith("ENGINEERING-DEFAULT")


def test_fsw_slot_hz_parsing() -> None:
    """f_sw 槽 '1.5MHz' → 1500kHz 进公式(槽给 Hz、缺省给 kHz,统一口径)。"""
    cat = {
        "up-sy8089_buck_3v3": BlockRecord(
            block_id="up-sy8089_buck_3v3", name="buck", desc="",
            electrical=Electrical(params={"f_sw": "1.5MHz"}, source="test"),
        )
    }
    ir = _ir([{"name": "5V", "voltage": 5.0}, {"name": "3V3", "voltage": 3.3, "imax": 1.0}])
    blocks = [{"block_id": "up-sy8089_buck_3v3", "instance": "bk1",
               "ports_binding": {"VIN": "5V", "3V3": "3V3", "GND": "GND"}}]
    advs = size_for_plan(blocks, ir=ir, catalog=cat)
    a = next(x for x in advs if x.kind == "buck-ripple")
    fsw = next(i for i in a.inputs if i[0] == "f_sw")
    assert fsw[1] == "1500kHz" and "参数槽" in fsw[2]
