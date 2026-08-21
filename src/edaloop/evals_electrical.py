"""P4-3 `eval --tier electrical`:注入式电气缺陷样本 harness(§9.4 Go 指标)。

不跑 E2E:直接构造 (DesignIR, BlockPlan, catalog) 三元组喂 validate() 全入口——
正样本(注入缺陷,必须逮住目标 code)× 负样本(干净/降级,绝不误杀阻断)。
Go:正负各 ≥5、电压错配/预算超载捕获 ≥9/10、负样本零误杀(错杀即 No-Go)。
14 需求零误伤由 smoke/daily 回归 E2E 单独实证(检查器 live 跑真计划)。
"""
from __future__ import annotations

import json
from pathlib import Path

from edaloop.generate.models import BlockPlan
from edaloop.intent.ir import DesignIR
from edaloop.knowledge.models import BlockRecord, Electrical
from edaloop.validate.checks import validate

_BLOCKING_ELECTRICAL = ("VOLTAGE_OUT_OF_RANGE", "RAIL_BUDGET_OVER")


def _ir(rails: list[dict]) -> DesignIR:
    return DesignIR.model_validate({"source": "eval-electrical.md", "power": {"rails": rails}})


def _plan(blocks: list[dict]) -> BlockPlan:
    return BlockPlan.model_validate({"blocks": blocks})


def _blk(instance: str, block_id: str, ports: dict[str, str]) -> dict:
    return {
        "block_id": block_id,
        "upstream_id": f"block.{block_id}",
        "instance": instance,
        "ports_binding": ports,
    }


def _cat(block_id: str, category: str, *, vmin=None, vmax=None, ityp=None, imax=None) -> BlockRecord:
    return BlockRecord(
        block_id=block_id,
        name=block_id,
        desc="",
        category=category,
        electrical=Electrical(v_supply_min=vmin, v_supply_max=vmax, i_typ=ityp, i_max=imax, source="eval"),
    )


def _samples() -> list[dict]:
    """每个样本:{name, kind: defect|clean, ir, plan, catalog, target(code)}。"""
    mcu = lambda i, net: _blk(i, "mcu-x", {"VDD": net, "GND": "GND"})  # noqa: E731
    mcu_cat = _cat("mcu-x", "mcu", vmin=3.0, vmax=3.6)

    return [
        # ---------- 正样本(缺陷注入,必须逮)----------
        dict(
            name="D1 标称轨过压:3.6V 上限 MCU 接 5V",
            kind="defect", target="VOLTAGE_OUT_OF_RANGE",
            ir=_ir([{"name": "5V", "voltage": 5.0}, {"name": "3V3", "voltage": 3.3}]),
            plan=_plan([mcu("u1", "5V")]),
            catalog={"mcu-x": mcu_cat},
        ),
        dict(
            name="D2 宽压上角:MCU(3.0-3.6)挂锂电池(3.0-4.2)",
            kind="defect", target="VOLTAGE_OUT_OF_RANGE",
            ir=_ir([{"name": "VBAT", "v_min": 3.0, "v_max": 4.2}]),
            plan=_plan([mcu("u1", "VBAT")]),
            catalog={"mcu-x": mcu_cat},
        ),
        dict(
            name="D3 宽压下角:块下限 3.4 高于轨下限 3.0",
            kind="defect", target="VOLTAGE_OUT_OF_RANGE",
            ir=_ir([{"name": "VDD", "v_min": 3.0, "v_max": 3.3}]),
            plan=_plan([mcu("u1", "VDD")]),
            catalog={"mcu-x": _cat("mcu-x", "mcu", vmin=3.4, vmax=3.6)},
        ),
        dict(
            name="D4 范围优先:块(2.0-5.2)对标称 5V 可过、对 4.5-5.5 上角越界",
            kind="defect", target="VOLTAGE_OUT_OF_RANGE",
            ir=_ir([{"name": "5V", "voltage": 5.0, "v_min": 4.5, "v_max": 5.5}]),
            plan=_plan([_blk("u1", "iface-x", {"VIN": "5V"})]),
            catalog={"iface-x": _cat("iface-x", "interface", vmin=2.0, vmax=5.2)},
        ),
        dict(
            name="D5 单边可判罪:只有 v_max=3.0 接 3.3V",
            kind="defect", target="VOLTAGE_OUT_OF_RANGE",
            ir=_ir([{"name": "3V3", "voltage": 3.3}]),
            plan=_plan([_blk("u1", "iface-x", {"VCC": "3V3"})]),
            catalog={"iface-x": _cat("iface-x", "interface", vmax=3.0)},
        ),
        dict(
            name="D6 家族兜底:IR 未声明的 12V 网喂 5.5V 上限块",
            kind="defect", target="VOLTAGE_OUT_OF_RANGE",
            ir=_ir([{"name": "3V3", "voltage": 3.3}]),
            plan=_plan([_blk("u1", "iface-x", {"VCC": "12V"})]),
            catalog={"iface-x": _cat("iface-x", "interface", vmin=2.0, vmax=5.5)},
        ),
        dict(
            name="D7 预算超载:Σ0.45A×1.2=0.54 > imax 0.5",
            kind="defect", target="RAIL_BUDGET_OVER",
            ir=_ir([{"name": "3V3", "voltage": 3.3, "imax": 0.5}]),
            plan=_plan([mcu("u1", "3V3"), _blk("u2", "iface-x", {"VCC": "3V3"})]),
            catalog={
                "mcu-x": _cat("mcu-x", "mcu", vmin=3.0, vmax=3.6, ityp=0.25),
                "iface-x": _cat("iface-x", "interface", vmin=3.3, vmax=5.0, ityp=0.2),
            },
        ),
        dict(
            name="D8 预算超载(单负载):0.3×1.2=0.36 > imax 0.3",
            kind="defect", target="RAIL_BUDGET_OVER",
            ir=_ir([{"name": "3V3", "voltage": 3.3, "imax": 0.3}]),
            plan=_plan([mcu("u1", "3V3")]),
            catalog={"mcu-x": _cat("mcu-x", "mcu", vmin=3.0, vmax=3.6, ityp=0.3)},
        ),
        dict(
            name="D9 未声明轨:plan 出现 IR 没有的 12V 网",
            kind="defect", target="UNDECLARED_RAIL",
            ir=_ir([{"name": "3V3", "voltage": 3.3}]),
            plan=_plan([_blk("u1", "buck-x", {"VIN": "12V", "3V3": "3V3"})]),
            catalog={"buck-x": _cat("buck-x", "power", vmin=4.5, vmax=40.0)},
        ),
        dict(
            name="D10 预算容量未知:轨无 imax → UNKNOWN 降级不静默",
            kind="defect", target="RAIL_BUDGET_UNKNOWN",
            ir=_ir([{"name": "3V3", "voltage": 3.3}]),
            plan=_plan([mcu("u1", "3V3")]),
            catalog={"mcu-x": _cat("mcu-x", "mcu", vmin=3.0, vmax=3.6, ityp=0.25)},
        ),
        # ---------- 负样本(干净/降级,绝不误杀)----------
        dict(
            name="C1 完整小板:5V→AMS1117→3V3→MCU 全兼容+预算内",
            kind="clean",
            ir=_ir([{"name": "5V", "voltage": 5.0, "imax": 1.0}, {"name": "3V3", "voltage": 3.3, "imax": 1.0}]),
            plan=_plan([
                _blk("ldo1", "ldo-ams1117-3v3", {"VIN5": "5V", "VOUT3V3": "3V3", "GND": "GND"}),
                mcu("u1", "3V3"),
            ]),
            catalog={
                "ldo-ams1117-3v3": _cat("ldo-ams1117-3v3", "power", vmin=4.5, vmax=15.0, ityp=0.01),
                "mcu-x": _cat("mcu-x", "mcu", vmin=3.0, vmax=3.6, ityp=0.25),
            },
        ),
        dict(
            name="C2 电源路径:bq24074 VBATT/VSYS 不当负载核对(反误杀地雷)",
            kind="clean",
            ir=_ir([{"name": "5V", "voltage": 5.0, "imax": 2.0}, {"name": "VBAT", "v_min": 3.0, "v_max": 4.2, "imax": 2.0}]),
            plan=_plan([
                _blk("chg1", "up-bq24074", {"VIN": "5V", "VBATT": "VBAT", "VSYS": "VBAT", "GND": "GND"}),
            ]),
            catalog={"up-bq24074": _cat("up-bq24074", "power", vmin=4.35, vmax=10.2, ityp=0.05)},
        ),
        dict(
            name="C3 buck 轨名输出端口 + 存储负载输入(反误杀地雷)",
            kind="clean",
            ir=_ir([{"name": "VIN", "voltage": 5.0, "imax": 1.0}, {"name": "3V3", "voltage": 3.3, "imax": 1.0}]),
            plan=_plan([
                _blk("bk1", "buck-x", {"VIN": "VIN", "3V3": "3V3", "GND": "GND"}),
                _blk("sd1", "sd-x", {"3V3": "3V3", "GND": "GND"}),
            ]),
            catalog={
                "buck-x": _cat("buck-x", "power", vmin=4.5, vmax=5.5, ityp=0.05),
                "sd-x": _cat("sd-x", "storage", vmin=2.7, vmax=3.6, ityp=0.1),
            },
        ),
        dict(
            name="C4 宽压全包:boost(2.0-24)VIN 挂 VBAT(3.0-4.2)拐角全覆盖",
            kind="clean",
            ir=_ir([{"name": "VBAT", "v_min": 3.0, "v_max": 4.2, "imax": 2.0}]),
            plan=_plan([_blk("bk1", "boost-mt3608", {"VIN": "VBAT", "VOUT5": "5V_ISO"})]),
            catalog={"boost-mt3608": _cat("boost-mt3608", "power", vmin=2.0, vmax=24.0, ityp=0.05)},
        ),
        dict(
            name="C5 边界相等:ch340n(3.3-5.0)接 3.3 与 5.0 都不越界",
            kind="clean",
            ir=_ir([{"name": "3V3", "voltage": 3.3, "imax": 1.0}, {"name": "5V", "voltage": 5.0, "imax": 1.0}]),
            plan=_plan([_blk("u1", "ch340-x", {"VCC": "3V3"}), _blk("u2", "ch340-x", {"VCC": "5V"})]),
            catalog={"ch340-x": _cat("ch340-x", "interface", vmin=3.3, vmax=5.0, ityp=0.03)},
        ),
        dict(
            name="C6 数据债降级:缺数据块只报弱告警不阻断",
            kind="clean",
            ir=_ir([{"name": "3V3", "voltage": 3.3}]),
            plan=_plan([_blk("e1", "eeprom-x", {"VCC": "3V3", "GND": "GND"})]),
            catalog={"eeprom-x": _cat("eeprom-x", "storage")},
        ),
    ]


def run_electrical_eval() -> dict:
    rows: list[dict] = []
    for s in _samples():
        findings = validate(s["ir"], s["plan"], None, catalog=s["catalog"])
        codes = [f.code for f in findings]
        if s["kind"] == "defect":
            ok = s["target"] in codes
            rows.append({"name": s["name"], "kind": "defect", "target": s["target"], "caught": ok, "codes": codes})
        else:
            # 干净样本:不得出现任何阻断性电气 finding(错杀即 No-Go);
            # 弱告警(数据债/预算 UNKNOWN/未声明轨)按三态降级语义放行,但单独计数供审视。
            killed = [c for c in _BLOCKING_ELECTRICAL if c in codes]
            weak_electrical = [c for c in ("ELECTRICAL_DATA_DEBT", "RAIL_BUDGET_UNKNOWN", "UNDECLARED_RAIL") if c in codes]
            rows.append({"name": s["name"], "kind": "clean", "killed": killed, "weak": weak_electrical, "codes": codes})
    defects = [r for r in rows if r["kind"] == "defect"]
    cleans = [r for r in rows if r["kind"] == "clean"]
    caught = sum(1 for r in defects if r["caught"])
    false_kills = sum(1 for r in cleans if r["killed"])
    # 9/10 口径只算"错配/超载"类(D1-D8);D9/D10 是告警语义样本单独计
    hard_defects = [r for r in defects if r["target"] in ("VOLTAGE_OUT_OF_RANGE", "RAIL_BUDGET_OVER")]
    hard_caught = sum(1 for r in hard_defects if r["caught"])
    summary = {
        "rows": rows,
        "defects_total": len(defects),
        "defects_caught": caught,
        "hard_defects_total": len(hard_defects),
        "hard_defects_caught": hard_caught,
        "catch_rate": hard_caught / len(hard_defects) if hard_defects else 0.0,
        "clean_total": len(cleans),
        "false_kills": false_kills,
    }
    summary["go"] = (hard_caught / len(hard_defects) >= 0.9 if hard_defects else False) and false_kills == 0
    for r in rows:
        if r["kind"] == "defect":
            print(f"[{'OK ' if r['caught'] else 'MISS'}] defect {r['name']} -> {r['target']}", flush=True)
        else:
            print(f"[{'OK ' if not r['killed'] else 'KILL'}] clean  {r['name']}" + (f" weak={r['weak']}" if r["weak"] else ""), flush=True)
    print(
        f"== electrical eval: 缺陷捕获 {caught}/{len(defects)}"
        f"(硬缺陷 {hard_caught}/{len(hard_defects)} = {summary['catch_rate']:.0%})"
        f", 负样本误杀 {false_kills}/{len(cleans)} -> {'Go' if summary['go'] else 'NO-GO'} ==",
        flush=True,
    )
    Path("runs/electrical-eval-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return summary
