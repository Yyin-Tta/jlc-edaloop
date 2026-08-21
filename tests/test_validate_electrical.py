from __future__ import annotations

from edaloop.generate.models import BlockPlan
from edaloop.intent.ir import DesignIR
from edaloop.knowledge.models import BlockRecord, Electrical
from edaloop.validate.checks import (
    check_current_budget,
    check_rails,
    check_voltage_compat,
    validate,
)
from edaloop.validate.models import STRONG_BLOCKING, is_blocking


def _ir(rails: list[dict]) -> DesignIR:
    return DesignIR.model_validate({"source": "t.md", "power": {"rails": rails}})


def _plan(blocks: list[dict]) -> BlockPlan:
    return BlockPlan.model_validate({"blocks": blocks})


def _cat(block_id: str, category: str, *, vmin=None, vmax=None, ityp=None, imax=None) -> BlockRecord:
    return BlockRecord(
        block_id=block_id,
        name=block_id,
        desc="",
        category=category,
        electrical=Electrical(
            v_supply_min=vmin, v_supply_max=vmax, i_typ=ityp, i_max=imax, source="test"
        ),
    )


def _blk(instance: str, block_id: str, ports: dict[str, str]) -> dict:
    return {
        "block_id": block_id,
        "upstream_id": f"block.{block_id}",
        "instance": instance,
        "ports_binding": ports,
    }


def _codes(findings) -> list[str]:
    return [f.code for f in findings]


# ---------- check_voltage_compat:负样本(必须逮住) ----------


def test_voltage_over_on_nominal_rail() -> None:
    """3.6V 上限的 MCU 接 5V 轨(标称)→ 逮。"""
    ir = _ir([{"name": "5V", "voltage": 5.0}, {"name": "3V3", "voltage": 3.3}])
    plan = _plan([_blk("u1", "mcu-x", {"VDD": "5V", "GND": "GND"})])
    cat = {"mcu-x": _cat("mcu-x", "mcu", vmin=3.0, vmax=3.6)}
    f = check_voltage_compat(ir, plan, cat)
    assert "VOLTAGE_OUT_OF_RANGE" in _codes(f)
    v = next(x for x in f if x.code == "VOLTAGE_OUT_OF_RANGE")
    assert not v.weak and v.severity == "error" and v.suggested_fix_class == "REPLAN"
    assert v.where.ref == "u1" and v.where.net == "5V"
    assert is_blocking(v)


def test_voltage_wide_corner_hi() -> None:
    """宽压上角:esp32(3.0-3.6)挂锂电池轨(3.0-4.2)→ bmax<rail.v_max 逮。"""
    ir = _ir([{"name": "VBAT", "v_min": 3.0, "v_max": 4.2}])
    plan = _plan([_blk("u1", "mcu-x", {"VDD": "VBAT"})])
    cat = {"mcu-x": _cat("mcu-x", "mcu", vmin=3.0, vmax=3.6)}
    assert "VOLTAGE_OUT_OF_RANGE" in _codes(check_voltage_compat(ir, plan, cat))


def test_voltage_wide_corner_lo() -> None:
    """宽压下角:块下限 3.4 高于轨下限 3.0(欠压角)→ 逮。"""
    ir = _ir([{"name": "VDD", "v_min": 3.0, "v_max": 3.3}])
    plan = _plan([_blk("u1", "mcu-x", {"VDD": "VDD"})])
    cat = {"mcu-x": _cat("mcu-x", "mcu", vmin=3.4, vmax=3.6)}
    assert "VOLTAGE_OUT_OF_RANGE" in _codes(check_voltage_compat(ir, plan, cat))


def test_voltage_range_overrides_nominal() -> None:
    """voltage 与范围并存取范围:块(2.0-5.2)对标称 5V 本可过,对范围 4.5-5.5 上角越界。"""
    ir = _ir([{"name": "5V", "voltage": 5.0, "v_min": 4.5, "v_max": 5.5}])
    plan = _plan([_blk("u1", "load-x", {"VIN": "5V"})])
    cat = {"load-x": _cat("load-x", "interface", vmin=2.0, vmax=5.2)}
    assert "VOLTAGE_OUT_OF_RANGE" in _codes(check_voltage_compat(ir, plan, cat))


def test_voltage_single_sided_max_provable() -> None:
    """只有 v_max=3.0 的块接 3.3V:bmax<轨压,可证明越界 → 逮(单边也能判罪)。"""
    ir = _ir([{"name": "3V3", "voltage": 3.3}])
    plan = _plan([_blk("u1", "iface-x", {"VCC": "3V3"})])
    cat = {"iface-x": _cat("iface-x", "interface", vmax=3.0)}
    assert "VOLTAGE_OUT_OF_RANGE" in _codes(check_voltage_compat(ir, plan, cat))


def test_voltage_name_family_fallback() -> None:
    """IR 未声明该轨,但网名自带电压(12V)→ 家族兜底仍强判。"""
    ir = _ir([{"name": "3V3", "voltage": 3.3}])
    plan = _plan([_blk("u1", "iface-x", {"VCC": "12V"})])
    cat = {"iface-x": _cat("iface-x", "interface", vmin=2.0, vmax=5.5)}
    f = check_voltage_compat(ir, plan, cat)
    v = next(x for x in f if x.code == "VOLTAGE_OUT_OF_RANGE")
    assert v.where.net == "12V"


# ---------- check_voltage_compat:正样本(绝不误杀——seeds 实证地雷) ----------


def test_ldo_output_port_not_checked() -> None:
    """ams1117 VOUT3V3 是输出侧:若按输入核对 4.5>3.3 必误杀,必须排除。"""
    ir = _ir([{"name": "5V", "voltage": 5.0}, {"name": "3V3", "voltage": 3.3}])
    plan = _plan([_blk("ldo1", "ldo-ams1117-3v3", {"VIN5": "5V", "VOUT3V3": "3V3", "GND": "GND"})])
    cat = {"ldo-ams1117-3v3": _cat("ldo-ams1117-3v3", "power", vmin=4.5, vmax=15.0)}
    assert "VOLTAGE_OUT_OF_RANGE" not in _codes(check_voltage_compat(ir, plan, cat))


def test_powerpath_charger_ports_not_inputs() -> None:
    """bq24074 VBATT/VSYS 是电池侧/系统输出侧:VBATT 按输入核对 4.35>3.7 必误杀。"""
    ir = _ir([{"name": "5V", "voltage": 5.0}, {"name": "VBAT", "v_min": 3.0, "v_max": 4.2}])
    plan = _plan(
        [_blk("chg1", "up-bq24074", {"VIN": "5V", "VBATT": "VBAT", "VSYS": "VBAT", "GND": "GND"})]
    )
    cat = {"up-bq24074": _cat("up-bq24074", "power", vmin=4.35, vmax=10.2)}
    assert "VOLTAGE_OUT_OF_RANGE" not in _codes(check_voltage_compat(ir, plan, cat))


def test_power_cat_rail_named_port_is_output() -> None:
    """电源类块的轨名端口(如 buck 输出就叫 3V3)是输出侧,不作负载核对。"""
    ir = _ir([{"name": "VIN", "voltage": 5.0}, {"name": "3V3", "voltage": 3.3}])
    plan = _plan([_blk("bk1", "buck-x", {"VIN": "VIN", "3V3": "3V3", "GND": "GND"})])
    cat = {"buck-x": _cat("buck-x", "power", vmin=4.5, vmax=5.5)}  # 若误当输入:4.5>3.3 误杀
    assert "VOLTAGE_OUT_OF_RANGE" not in _codes(check_voltage_compat(ir, plan, cat))


def test_load_cat_rail_named_port_is_input() -> None:
    """非电源类(存储)的 3V3 端口是真输入,照常核对。"""
    ir = _ir([{"name": "3V3", "voltage": 3.3}])
    plan = _plan([_blk("sd1", "sd-x", {"3V3": "3V3", "GND": "GND"})])
    cat = {"sd-x": _cat("sd-x", "storage", vmin=2.7, vmax=3.6)}
    assert check_voltage_compat(ir, plan, cat) == []


def test_voltage_boundaries_pass() -> None:
    """边界相等不算越界:ch340n(3.3-5.0)接 3.3V 与 5V 都过。"""
    ir = _ir([{"name": "3V3", "voltage": 3.3}, {"name": "5V", "voltage": 5.0}])
    plan = _plan(
        [_blk("u1", "ch340", {"VCC": "3V3"}), _blk("u2", "ch340", {"VCC": "5V"})]
    )
    cat = {"ch340": _cat("ch340", "interface", vmin=3.3, vmax=5.0)}
    assert "VOLTAGE_OUT_OF_RANGE" not in _codes(check_voltage_compat(ir, plan, cat))


def test_unresolvable_net_is_unknown_not_debt() -> None:
    """网名猜不出电压(PWR_RAIL)→ UNKNOWN 不进强判定,也不算数据债。"""
    ir = _ir([{"name": "3V3", "voltage": 3.3}])
    plan = _plan([_blk("u1", "mcu-x", {"VDD": "PWR_RAIL"})])
    cat = {"mcu-x": _cat("mcu-x", "mcu", vmin=3.0, vmax=3.6)}
    assert check_voltage_compat(ir, plan, cat) == []


def test_missing_block_data_registers_debt_weak() -> None:
    """块缺 v_supply 数据 → 数据债弱告警(不静默、不阻断)。"""
    ir = _ir([{"name": "3V3", "voltage": 3.3}])
    plan = _plan([_blk("u1", "eeprom-x", {"VCC": "3V3", "GND": "GND"})])
    cat = {"eeprom-x": _cat("eeprom-x", "storage")}  # 无 electrical 数值
    f = check_voltage_compat(ir, plan, cat)
    codes = _codes(f)
    assert "ELECTRICAL_DATA_DEBT" in codes and "VOLTAGE_OUT_OF_RANGE" not in codes
    debt = next(x for x in f if x.code == "ELECTRICAL_DATA_DEBT")
    assert debt.weak and not is_blocking(debt) and "u1" in debt.evidence


def test_gnd_net_skipped() -> None:
    ir = _ir([{"name": "3V3", "voltage": 3.3}])
    plan = _plan([_blk("u1", "mcu-x", {"VDD": "3V3", "GND": "GND"})])
    cat = {"mcu-x": _cat("mcu-x", "mcu", vmin=3.0, vmax=3.6)}
    assert check_voltage_compat(ir, plan, cat) == []


# ---------- check_current_budget 三态 ----------


def _budget_ir(imax: float | None) -> DesignIR:
    return _ir([{"name": "3V3", "voltage": 3.3, "imax": imax}])


def test_budget_over() -> None:
    """Σ0.45×1.2=0.54 > imax 0.5 → RAIL_BUDGET_OVER 强。"""
    ir = _budget_ir(0.5)
    plan = _plan(
        [_blk("u1", "mcu-x", {"VDD": "3V3"}), _blk("u2", "iface-x", {"VCC": "3V3"})]
    )
    cat = {
        "mcu-x": _cat("mcu-x", "mcu", vmin=3.0, vmax=3.6, ityp=0.25),
        "iface-x": _cat("iface-x", "interface", vmin=3.3, vmax=5.0, ityp=0.2),
    }
    f = check_current_budget(ir, plan, cat)
    over = next(x for x in f if x.code == "RAIL_BUDGET_OVER")
    assert not over.weak and over.severity == "error" and is_blocking(over)
    assert "0.54" in over.evidence and "u1" in over.evidence


def test_budget_ok_no_finding() -> None:
    ir = _budget_ir(1.0)
    plan = _plan(
        [_blk("u1", "mcu-x", {"VDD": "3V3"}), _blk("u2", "iface-x", {"VCC": "3V3"})]
    )
    cat = {
        "mcu-x": _cat("mcu-x", "mcu", ityp=0.25),
        "iface-x": _cat("iface-x", "interface", ityp=0.2),
    }
    assert check_current_budget(ir, plan, cat) == []


def test_budget_unknown_capacity() -> None:
    """轨容量未知 → UNKNOWN 弱告警。"""
    ir = _budget_ir(None)
    plan = _plan([_blk("u1", "mcu-x", {"VDD": "3V3"})])
    cat = {"mcu-x": _cat("mcu-x", "mcu", ityp=0.25)}
    f = check_current_budget(ir, plan, cat)
    u = next(x for x in f if x.code == "RAIL_BUDGET_UNKNOWN")
    assert u.weak and "imax 未知" in u.evidence and not is_blocking(u)


def test_budget_unknown_coverage_with_missing_list() -> None:
    """负载覆盖率<100% → UNKNOWN,evidence 带覆盖率% 与缺数块清单。"""
    ir = _budget_ir(1.0)
    plan = _plan(
        [_blk("u1", "mcu-x", {"VDD": "3V3"}), _blk("u2", "noi-x", {"VCC": "3V3"})]
    )
    cat = {"mcu-x": _cat("mcu-x", "mcu", ityp=0.25), "noi-x": _cat("noi-x", "interface")}
    f = check_current_budget(ir, plan, cat)
    u = next(x for x in f if x.code == "RAIL_BUDGET_UNKNOWN")
    assert "50%" in u.evidence and "u2" in u.evidence and u.weak


def test_budget_same_rail_dedup() -> None:
    """同块 VDD+VDDA 双口绑同一轨只计一次负载。"""
    ir = _budget_ir(0.4)
    plan = _plan([_blk("u1", "mcu-x", {"VDD": "3V3", "VDDA": "3V3"})])
    cat = {"mcu-x": _cat("mcu-x", "mcu", ityp=0.25)}
    f = check_current_budget(ir, plan, cat)
    assert "RAIL_BUDGET_OVER" not in _codes(f)  # 0.25×1.2=0.3 ≤ 0.4;若重复计 0.6>0.4 必误报


def test_budget_dual_rail_block_counts_both() -> None:
    ir = _ir(
        [
            {"name": "5V", "voltage": 5.0, "imax": 1.0},
            {"name": "3V3", "voltage": 3.3, "imax": 1.0},
        ]
    )
    plan = _plan([_blk("u1", "iface-x", {"VIN5": "5V", "VCC": "3V3"})])
    cat = {"iface-x": _cat("iface-x", "interface", vmin=3.3, vmax=5.0, ityp=0.2)}
    f = check_current_budget(ir, plan, cat)
    assert _codes(f) == []  # 两轨各计 0.2×1.2=0.24,均在预算内


def test_budget_no_rails_no_findings() -> None:
    ir = _ir([])
    plan = _plan([_blk("u1", "mcu-x", {"VDD": "3V3"})])
    cat = {"mcu-x": _cat("mcu-x", "mcu", ityp=0.25)}
    assert check_current_budget(ir, plan, cat) == []


def test_budget_regulator_output_not_load() -> None:
    """稳压器输出侧不占轨负载:buck 出 3V3 只算其 VIN 侧 5V 的负载。"""
    ir = _ir(
        [
            {"name": "5V", "voltage": 5.0, "imax": 0.3},
            {"name": "3V3", "voltage": 3.3, "imax": 0.3},
        ]
    )
    plan = _plan(
        [
            _blk("bk1", "buck-x", {"VIN": "5V", "3V3": "3V3"}),
            _blk("u1", "mcu-x", {"VDD": "3V3"}),
        ]
    )
    cat = {
        "buck-x": _cat("buck-x", "power", vmin=4.5, vmax=5.5, ityp=0.05),
        "mcu-x": _cat("mcu-x", "mcu", ityp=0.25),
    }
    f = check_current_budget(ir, plan, cat)
    # 3V3 轨只挂 mcu 0.25×1.2=0.3 ≤ 0.3 不越界;buck 的 3V3 输出口不算负载
    assert "RAIL_BUDGET_OVER" not in _codes(f)


# ---------- check_rails 双向化(P4-3③) ----------


def test_undeclared_rail_warns_weak() -> None:
    """plan 出现 IR 未声明的轨样网(12V)→ UNDECLARED_RAIL 弱告警。"""
    ir = _ir([{"name": "3V3", "voltage": 3.3}])
    plan = _plan([_blk("u1", "iface-x", {"VIN": "12V", "GND": "GND"})])
    f = check_rails(ir, plan)
    u = next(x for x in f if x.code == "UNDECLARED_RAIL")
    assert u.weak and u.where.net == "12V" and not is_blocking(u)


def test_undeclared_rail_family_match_no_warn() -> None:
    """网名家族命中已声明轨(5V_ISO ↔ VISO)→ 不告;猜不出电压的网(PWR_IN)→ 不告。"""
    ir = _ir([{"name": "5V_ISO", "voltage": 5.0}, {"name": "3V3", "voltage": 3.3}])
    plan = _plan(
        [_blk("u1", "iso-x", {"VIN": "VISO", "AUX": "PWR_IN", "VDD": "3V3", "GND": "GND"})]
    )
    assert check_rails(ir, plan) == []


# ---------- 接线与死占位清理 ----------


def test_validate_wires_electrical_checkers() -> None:
    ir = _ir([{"name": "5V", "voltage": 5.0}])
    plan = _plan([_blk("u1", "mcu-x", {"VDD": "5V"})])
    cat = {"mcu-x": _cat("mcu-x", "mcu", vmin=3.0, vmax=3.6)}
    f = validate(ir, plan, None, catalog=cat)
    assert "VOLTAGE_OUT_OF_RANGE" in _codes(f)


def test_wrong_net_dead_placeholder_removed() -> None:
    """P4-3③:WRONG_NET 死占位清出 STRONG_BLOCKING(无任何检查器产它)。"""
    assert "WRONG_NET" not in STRONG_BLOCKING


def test_battery_pad_aliases_family_match() -> None:
    """锂电保护自由拓扑 planner 用 B+/B- 命名电池焊盘(req-08 实测)→ VBAT/GND 家族,不再误报 MISSING_RAIL。"""
    ir = _ir([{"name": "VBAT", "v_min": 3.0, "v_max": 4.2}])
    plan = _plan([_blk("prot1", "battery-dw01-protection", {"VDD": "B+", "VSS": "B-"})])
    assert check_rails(ir, plan) == []
