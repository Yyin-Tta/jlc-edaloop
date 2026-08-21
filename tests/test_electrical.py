from __future__ import annotations

import json

from edaloop.knowledge.electrical import map_params, parse_current, parse_v_range


def test_parse_v_range_forms() -> None:
    assert parse_v_range("2V~3.6V") == (2.0, 3.6)
    assert parse_v_range("4.5 V ~ 5.5 V") == (4.5, 5.5)
    assert parse_v_range("5V") == (5.0, 5.0)
    assert parse_v_range("2.7V-5.5V") == (2.7, 5.5)
    assert parse_v_range("-") is None


def test_parse_current_units() -> None:
    assert parse_current("1A") == 1.0
    assert parse_current("500mA") == 0.5
    assert parse_current("1.5 A") == 1.5
    assert parse_current("5uA") == 5e-06
    assert parse_current("n/a") is None


def test_map_power_category() -> None:
    m = map_params(
        {
            "Voltage - Supply": "2V~3.6V",
            "Output Current": "1A",
            "Quiescent Current": "5mA",
            "Operating Temperature": "-40~85C",
        },
        "power",
    )
    assert m["v_supply_min"] == 2.0 and m["v_supply_max"] == 3.6
    assert m["i_max"] == 1.0 and m["i_typ"] == 0.005
    assert "Operating Temperature=-40~85C" in m["unmapped"]


def test_map_driver_ic_as_imax() -> None:
    m = map_params({"Ic": "500mA", "Voltage - Supply": "5V"}, "driver")
    assert m["i_max"] == 0.5
    assert m["v_supply_min"] == 5.0 and m["v_supply_max"] == 5.0


def test_map_mcu_output_current_not_imax() -> None:
    """非电源类的 Output Current(如 MCU IO 驱动能力)不强映射为 i_max,留人工判。"""
    m = map_params({"Output Current": "20mA"}, "mcu")
    assert "i_max" not in m
    assert "Output Current=20mA" in m["unmapped"]


def test_apply_merges_without_overwrite(tmp_path) -> None:
    from edaloop.knowledge import electrical as elec
    from edaloop.knowledge.models import BlockRecord

    seeds = tmp_path / "blocks.jsonl"
    seeds.write_text(
        json.dumps(
            {
                "block_id": "ldo-x",
                "name": "LDO",
                "desc": "d",
                "electrical": {"i_max": 1.5, "source": "datasheet §7"},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    proposal = tmp_path / "p.jsonl"
    proposal.write_text(
        json.dumps(
            {
                "block_id": "ldo-x",
                "lcsc": "C6186",
                "proposed": {"v_supply_min": 4.5, "v_supply_max": 5.5},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    rc = elec.main(["apply", "--proposal", str(proposal), "--seeds", str(seeds)])
    assert rc == 0
    b = BlockRecord.model_validate(json.loads(seeds.read_text(encoding="utf-8")))
    assert b.electrical is not None
    assert b.electrical.i_max == 1.5  # 旧值不被覆盖
    assert b.electrical.v_supply_max == 5.5  # 空槽被填
    assert "C6186" in b.electrical.source  # 溯源更新为 wmsc
