from __future__ import annotations

from edaloop.generate.sizing import (
    buck_ripple,
    divider_network,
    fuse_rating,
    led_series_resistor,
    reg_caps,
    size_for_plan,
    thermal_check,
    tvs_rating,
    _e24,
    _fmt_ohm,
)


def test_e24_normalization() -> None:
    v, _ = _e24(233.0)
    assert v == 240.0
    v2, _ = _e24(4630.0)
    assert abs(v2 - 4700) < 1


def test_led_resistor_basic() -> None:
    a = led_series_resistor(3.3, 2.0, 5)
    assert abs(a.result_raw - 260.0) < 1
    assert "270" in a.result_rec  # E24 归一


def test_led_resistor_impossible() -> None:
    a = led_series_resistor(1.8, 2.0, 10)
    assert a.result_raw < 0
    assert any("不可行" in n for n in a.notes)


def test_divider_basic() -> None:
    a = divider_network(5.0, 3.0, 10000)
    assert abs(a.result_raw - 6000) < 1
    assert "Rtop" in a.result_rec and "Rbot" in a.result_rec
    assert any("误差" in n for n in a.notes)


def test_divider_invalid() -> None:
    a = divider_network(5.0, 7.0, 10000)
    assert a.result_rec == "n/a"


def test_reg_caps_ldo() -> None:
    a = reg_caps("ldo", 800)
    assert any("C_OUT" in n for n in a.notes)
    assert any("不可省" in n for n in a.notes)


def test_reg_caps_unknown() -> None:
    assert reg_caps("linear-reg", 100).result_rec == "n/a"


def test_size_for_plan_led_and_ldo() -> None:
    advices = size_for_plan(
        [
            {"block_id": "led-indicator", "instance": "led1", "ports_binding": {"CTRL": "3V3", "GND": "GND"}},
            {"block_id": "ldo-ams1117-3v3", "instance": "ldo1", "ports_binding": {"VIN_5V": "5V", "3V3": "3V3"}},
        ]
    )
    kinds = {a.kind for a in advices}
    assert "led-resistor" in kinds
    assert "reg-caps" in kinds
    led = next(a for a in advices if a.kind == "led-resistor")
    assert led.target == "led1"


def test_size_for_plan_conservative() -> None:
    advices = size_for_plan(
        [{"block_id": "mcu-stm32", "instance": "u1", "ports_binding": {}}]
    )
    assert advices == []


def test_fmt_ohm() -> None:
    assert _fmt_ohm(4700) == "4.7k"
    assert _fmt_ohm(270.0) == "270"
    assert _fmt_ohm(1_000_000) == "1M"


def test_buck_ripple_solves_inductor() -> None:
    a = buck_ripple(3.3, 12.0, 1000, 500)
    assert 5 <= a.result_raw <= 25  # 3.3V/1A/500kHz 典型 10µH 量级
    assert "µH" in a.result_rec or "H" in a.result_rec
    assert any("饱和电流" in n for n in a.notes)


def test_buck_ripple_invalid() -> None:
    assert buck_ripple(12.0, 3.3, 1000, 500).result_rec == "n/a"


def test_tvs_rating() -> None:
    a = tvs_rating(24.0)
    assert abs(a.result_raw - 26.4) < 0.1
    assert "VRWM" in a.result_rec


def test_fuse_rating() -> None:
    a = fuse_rating(2000)
    assert abs(a.result_raw - 2.5) < 0.01
    assert "PPTC" in a.result_rec


def test_thermal_check_margin() -> None:
    ok = thermal_check(0.5, 60.0, 25.0)
    assert "裕量" in ok.result_rec
    hot = thermal_check(2.0, 60.0, 55.0)
    assert any("不足" in n for n in hot.notes)


def test_size_for_plan_covers_buck_full() -> None:
    """P4-4①:轨输入走 IR(零硬编码),空绑定+无 IR 只剩降级记号。"""
    from edaloop.intent.ir import DesignIR

    ir = DesignIR.model_validate(
        {"source": "t.md", "power": {"rails": [
            {"name": "VIN", "v_min": 8.0, "v_max": 36.0, "imax": 1.0},
            {"name": "5V", "voltage": 5.0},
        ]}}
    )
    advices = size_for_plan(
        [
            {"block_id": "up-xl1509_buck_12v_5v", "instance": "b1",
             "ports_binding": {"VIN": "VIN", "+5V": "5V", "GND": "GND"}},
            {"block_id": "up-vehicle_input_tps54360_5v", "instance": "vin1",
             "ports_binding": {"VIN": "VIN", "GND": "GND"}},
        ],
        ir=ir,
    )
    real = {a.kind for a in advices if a.result_rec != "n/a"}
    assert "buck-ripple" in real
    assert "tvs-rating" in real
    assert "fuse-rating" in real
    # 输入来源表:每条真实建议的轨输入都有出处(硬编码清零断言的基础形态)
    for a in advices:
        if a.result_rec != "n/a":
            assert a.gap() is None, f"{a.kind}@{a.target} 轨输入缺出处"
    # 无 IR + 空绑定:轨相关公式(纹波)不再凭关键词硬套 12V,只留降级记号
    # (电容惯例值/保险丝不依赖轨,仍可用工程缺省给出)
    degraded = size_for_plan(
        [{"block_id": "up-xl1509_buck_12v_5v", "instance": "b1", "ports_binding": {}}]
    )
    ripple = [a for a in degraded if a.kind == "buck-ripple"]
    assert ripple and all(a.result_rec == "n/a" for a in ripple)
