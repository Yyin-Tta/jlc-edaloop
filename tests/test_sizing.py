from __future__ import annotations

from edaloop.generate.sizing import (
    divider_network,
    led_series_resistor,
    reg_caps,
    size_for_plan,
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
