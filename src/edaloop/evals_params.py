"""P4-4 `eval --tier params`: 参数核对闭环注入式 harness(§9.4 P4-4 Go 指标)。

不跑 E2E:确定性部分直接构造 (DesignIR, BlockPlan, catalog) 三元组,
size_for_plan → validate(sizing=...) 全入口;critic 部分用真 LLM 跑注入缺陷设计。

Go(§9 line 372):
  A. 注入 ≥3 个错值 plan,PARAM_OFF_SPEC 拦截 ≥2;
  B. 干净样本零误杀(E24 邻档/电容超规格/去耦共存/缺件/无轨降级/轨直挂歧义);
  C. 电源类块参数建议覆盖 ≥80%(种子库 power 类全量,合成轨跑 size_for_plan);
  D. 输入来源表完备:全部真实建议轨输入零空出处(硬编码清零断言可执行);
  E. critic 缺陷捕获 ≥4/5(真 LLM;密钥缺失记 skipped 不进 Go)。
14 需求零误伤由 smoke/daily 回归 E2E 单独实证(检查器 live 跑真计划)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # GBK 控制台防 'µ' 炸打印

from edaloop.generate.models import BlockPlan
from edaloop.generate.sizing import size_for_plan
from edaloop.intent.ir import DesignIR
from edaloop.knowledge.models import BlockRecord
from edaloop.validate.checks import validate

_STD_CATALOG = {
    "resistor-std": BlockRecord(
        block_id="resistor-std", name="标准电阻", desc="params.value 查标准件表",
        category="passive", tags=["std-value", "resistor"], ports=["1", "2"], pinout={"1": "1", "2": "2"},
    ),
    "capacitor-std": BlockRecord(
        block_id="capacitor-std", name="标准电容", desc="params.value 查标准件表",
        category="passive", tags=["std-value", "capacitor"], ports=["1", "2"], pinout={"1": "1", "2": "2"},
    ),
    "led-indicator": BlockRecord(
        block_id="led-indicator", name="LED 指示灯", desc="GPIO 驱动 LED(块内含限流电阻)",
        category="indicator", tags=["led"], ports=["DRV", "GND"],
    ),
    "up-sy8089_buck_3v3": BlockRecord(
        block_id="up-sy8089_buck_3v3", name="SY8089 buck 3V3", desc="5V→3.3V buck",
        category="power", tags=["buck", "sy8089"], ports=["VIN", "3V3", "GND"],
    ),
}


def _ir(rails: list[dict]) -> DesignIR:
    return DesignIR.model_validate({"source": "eval-params.md", "power": {"rails": rails}})


def _plan(blocks: list[dict]) -> BlockPlan:
    return BlockPlan.model_validate({"blocks": blocks})


def _up_blk(instance: str, block_id: str, ports: dict[str, str]) -> dict:
    return {"block_id": block_id, "upstream_id": f"block.{block_id}", "instance": instance, "ports_binding": ports}


def _std_rc(instance: str, kind: str, value: str, net1: str, net2: str) -> dict:
    return {
        "block_id": f"{kind}-std", "instance": instance,
        "pins_binding": {"1": net1, "2": net2}, "params": {"value": value},
    }


def _run(ir: DesignIR, plan: BlockPlan) -> list[str]:
    """size_for_plan + validate 全入口 → finding codes(P4-4① 轮内化路径)。"""
    advices = size_for_plan(plan.blocks, ir=ir, catalog=_STD_CATALOG)
    findings = validate(ir, plan, None, catalog=_STD_CATALOG, sizing=advices or None)
    return [f.code for f in findings]


def _param_samples() -> list[dict]:
    """{name, kind: defect|clean, ir, plan, expect: PARAM_OFF_SPEC 出现( defect)/不出现( clean)}。"""
    single_3v3 = _ir([{"name": "3V3", "voltage": 3.3}])
    buck_rails = _ir([{"name": "5V", "voltage": 5.0}, {"name": "3V3", "voltage": 3.3, "imax": 1.0}])
    return [
        # ---------- 正样本(错值注入,PARAM_OFF_SPEC 必须出现)----------
        dict(
            name="D1 GPIO 驱动 LED 限流 10k vs 建议 270(单轨推断,通道 B 网相交)",
            kind="defect",
            ir=single_3v3,
            plan=_plan([
                _up_blk("led1", "led-indicator", {"DRV": "NET_LED1", "GND": "GND"}),
                _std_rc("r1", "resistor", "10k", "NET_LED1", "NET_LED_A"),
            ]),
        ),
        dict(
            name="D2 buck 输出电容 100n vs 建议 2.7µ(1A 纹波公式,通道 B 网对相等)",
            kind="defect",
            ir=buck_rails,
            plan=_plan([
                _up_blk("b1", "up-sy8089_buck_3v3", {"VIN": "5V", "3V3": "3V3", "GND": "GND"}),
                _std_rc("c1", "capacitor", "100n", "3V3", "GND"),
            ]),
        ),
        dict(
            name="D3 混装判别:两只 GPIO LED 一对(330)一错(47k),只错者被标",
            kind="defect", expect_count=1,
            ir=single_3v3,
            plan=_plan([
                _up_blk("led1", "led-indicator", {"DRV": "NET_LED1", "GND": "GND"}),
                _std_rc("r1", "resistor", "330", "NET_LED1", "NET_LED_A"),
                _up_blk("led2", "led-indicator", {"DRV": "NET_LED2", "GND": "GND"}),
                _std_rc("r2", "resistor", "47k", "NET_LED2", "NET_LED_B"),
            ]),
        ),
        # ---------- 负样本(干净,PARAM_OFF_SPEC 绝不出现)----------
        dict(
            name="C1 GPIO LED 限流取建议 E24 邻档 330(容差内)",
            kind="clean",
            ir=single_3v3,
            plan=_plan([
                _up_blk("led1", "led-indicator", {"DRV": "NET_LED1", "GND": "GND"}),
                _std_rc("r1", "resistor", "330", "NET_LED1", "NET_LED_A"),
            ]),
        ),
        dict(
            name="C2 buck 输出 22µ 超规格(工程裕量)+ 100n 去耦共存同网对",
            kind="clean",
            ir=buck_rails,
            plan=_plan([
                _up_blk("b1", "up-sy8089_buck_3v3", {"VIN": "5V", "3V3": "3V3", "GND": "GND"}),
                _std_rc("c1", "capacitor", "100n", "3V3", "GND"),
                _std_rc("c2", "capacitor", "22u", "3V3", "GND"),
            ]),
        ),
        dict(
            name="C3 缺件不判:LED 块内自带限流,图上无外部 std R",
            kind="clean",
            ir=single_3v3,
            plan=_plan([_up_blk("led1", "led-indicator", {"DRV": "NET_LED1", "GND": "GND"})]),
        ),
        dict(
            name="C4 无轨降级:建议 n/a(rec_value 空)不进比对",
            kind="clean",
            ir=_ir([]),
            plan=_plan([
                _up_blk("led1", "led-indicator", {"DRV": "NET_LED1", "GND": "GND"}),
                _std_rc("r1", "resistor", "10k", "NET_LED1", "NET_LED_A"),
            ]),
        ),
        dict(
            name="C5 轨直挂 LED 歧义不判:驱动网=3V3 轨,同节点上拉 R 与限流拓扑不可分",
            kind="clean",
            ir=single_3v3,
            plan=_plan([
                _up_blk("led1", "led-indicator", {"DRV": "3V3", "GND": "GND"}),
                _std_rc("r2", "resistor", "10k", "3V3", "EN"),
            ]),
        ),
        dict(
            name="C6 无关 std R 不判:未触任何建议网(MCU 复位上拉)",
            kind="clean",
            ir=buck_rails,
            plan=_plan([
                _up_blk("b1", "up-sy8089_buck_3v3", {"VIN": "5V", "3V3": "3V3", "GND": "GND"}),
                _std_rc("r1", "resistor", "10k", "3V3", "EN"),
            ]),
        ),
    ]


# ---- E. critic 缺陷捕获(真 LLM) ----

_CRITIC_SAMPLES = [
    dict(
        name="K1 MCU 模组无去耦",
        expect="CRITIC_DECOUPLING",
        blocks=[
            {"block_id": "mcu-esp32s3", "instance": "u1",
             "ports_binding": {"3V3": "3V3", "GND": "GND", "EN": "EN", "IO0": "IO0"}},
        ],
        catalog_desc={"mcu-esp32s3": "ESP32-S3 模组最小系统,3V3 供电,无内置去耦电容"},
        netlist="nets: 3V3, GND, EN, IO0(无任何 100nF 去耦网)",
    ),
    dict(
        name="K2 I2C SDA/SCL 无上拉",
        expect="CRITIC_PULL_RESISTORS",
        blocks=[
            {"block_id": "mcu-esp32s3", "instance": "u1",
             "ports_binding": {"3V3": "3V3", "SDA": "I2C_SDA", "SCL": "I2C_SCL", "GND": "GND"}},
        ],
        catalog_desc={"mcu-esp32s3": "ESP32-S3 模组,GPIO 开漏 I2C 主机"},
        netlist="nets: 3V3, GND, I2C_SDA, I2C_SCL(SDA/SCL 上无任何电阻件)",
    ),
    dict(
        name="K3 RS-485 端子无 ESD/TVS",
        expect="CRITIC_INTERFACE_PROTECTION",
        blocks=[
            {"block_id": "rs485-sp3485", "instance": "u1",
             "ports_binding": {"3V3": "3V3", "A": "RS485_A", "B": "RS485_B", "GND": "GND"}},
        ],
        catalog_desc={"rs485-sp3485": "SP3485 RS-485 收发器,A/B 直连接线端子长线"},
        netlist="nets: 3V3, GND, RS485_A, RS485_B(A/B 直出端子,无 TVS/ESD 器件)",
    ),
    dict(
        name="K4 12V 继电器感性负载无续流二极管",
        expect="CRITIC_INTERFACE_PROTECTION",
        blocks=[
            {"block_id": "nmos-driver", "instance": "q1",
             "ports_binding": {"G": "MCU_RELAY", "D": "RELAY_COIL", "S": "GND"}},
        ],
        catalog_desc={"nmos-driver": "NMOS 低边驱动 12V 继电器线圈(感性负载)"},
        netlist="nets: 12V, RELAY_COIL, MCU_RELAY, GND(线圈两端无续流二极管)",
    ),
    dict(
        name="K5 LDO 5V→3V3 带 800mA 负载无散热",
        expect="CRITIC_THERMAL",
        blocks=[
            {"block_id": "ldo-ams1117", "instance": "u1",
             "ports_binding": {"VIN": "5V", "VOUT": "3V3", "GND": "GND"}},
        ],
        catalog_desc={"ldo-ams1117": "AMS1117-3.3 线性稳压,压差 1.7V,SOT-223 无散热铺铜"},
        netlist="nets: 5V, 3V3, GND",
        rails="3V3=3.3V(imax=0.8A), 5V=5.0V",
    ),
]


def _critic_eval() -> dict:
    import os

    from edaloop.loop.critic import review_plan

    if not (os.environ.get("EDALOOP_LLM_KEY") or os.environ.get("OPENAI_API_KEY")):
        return {"skipped": True, "reason": "EDALOOP_LLM_KEY 未配置(critic 需真 LLM)"}
    from edaloop.llm.openai_compat import get_llm

    llm = get_llm()
    rows = []
    for s in _CRITIC_SAMPLES:
        plan = BlockPlan.model_validate({"blocks": s["blocks"]})
        try:
            findings = review_plan(
                plan, llm, catalog_desc=s.get("catalog_desc"),
                netlist_summary=s.get("netlist", ""), rails_summary=s.get("rails", ""),
                sizing_summary="", attempts=2,
            )
        except RuntimeError as e:  # 解析失败按未捕获计,不让单样本炸整批
            rows.append({"name": s["name"], "expect": s["expect"], "caught": False, "codes": [f"ERR:{e}"]})
            continue
        codes = [f.code for f in findings]
        caught = s["expect"] in codes
        rows.append({"name": s["name"], "expect": s["expect"], "caught": caught, "codes": codes})
        print(f"[{'OK ' if caught else 'MISS'}] critic {s['name']} -> {s['expect']} 实际 {codes}", flush=True)
    caught_n = sum(1 for r in rows if r["caught"])
    return {"skipped": False, "rows": rows, "total": len(rows), "caught": caught_n}


# ---- C/D. 电源块覆盖 + 输入来源表完备(种子库全量,离线确定性) ----


def _power_coverage() -> dict:
    from edaloop.validate.checks import _family_volts, _rail_family

    p = Path("seeds/blocks.jsonl")
    if not p.exists():
        p = Path(__file__).resolve().parents[2] / "seeds" / "blocks.jsonl"
    records = [BlockRecord.model_validate(json.loads(l)) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    catalog = {r.block_id: r for r in records}
    rows, hardcode_gaps = [], []
    for rec in records:
        if rec.category != "power":
            continue
        # 合成绑定:upstream 默认网;无 upstream 的种子块用端口名当网名
        binding = dict(rec.upstream.ports) if rec.upstream else {port: port for port in rec.ports}
        # 合成 IR 轨:绑定网里家族可解的逐个声明(电池家族给 3.0-4.2 宽压窗)
        rails: list[dict] = []
        for net in set(binding.values()):
            fam = _rail_family(net)
            if fam.endswith("|gnd"):
                continue
            v = _family_volts(fam)
            if v is None:
                continue
            rails.append(
                {"name": net, "v_min": 3.0, "v_max": 4.2} if abs(v - 3.7) < 0.3 else {"name": net, "voltage": v}
            )
        ir = DesignIR.model_validate({"source": "coverage.md", "power": {"rails": rails}}) if rails else DesignIR.model_validate({"source": "coverage.md"})
        block = {"block_id": rec.block_id, "instance": "x1", "ports_binding": binding}
        advices = size_for_plan([block], ir=ir, catalog=catalog)
        real = [a for a in advices if a.result_rec != "n/a"]
        for a in real:
            gap = a.gap()
            if gap:
                hardcode_gaps.append(f"{rec.block_id}:{a.kind}({gap})")
        rows.append({"block_id": rec.block_id, "advice_kinds": [a.kind for a in real],
                     "degraded_kinds": [a.kind for a in advices if a.result_rec == "n/a"]})
    covered = [r for r in rows if r["advice_kinds"]]
    summary = {
        "rows": rows,
        "power_total": len(rows),
        "power_covered": len(covered),
        "coverage": len(covered) / len(rows) if rows else 0.0,
        "uncovered": [r["block_id"] for r in rows if not r["advice_kinds"]],
        "hardcode_gaps": hardcode_gaps,
    }
    for r in rows:
        tag = "OK " if r["advice_kinds"] else "MISS"
        print(f"[{tag}] power {r['block_id']} -> {r['advice_kinds'] or '(无建议)'}"
              + (f" 降级 {r['degraded_kinds']}" if r["degraded_kinds"] else ""), flush=True)
    return summary


def _critic_str(critic: dict) -> str:
    if critic.get("skipped"):
        return "skipped"
    return f"{critic['caught']}/{critic['total']}"


def run_params_eval() -> dict:
    rows: list[dict] = []
    for s in _param_samples():
        codes = _run(s["ir"], s["plan"])
        flagged = "PARAM_OFF_SPEC" in codes
        if s["kind"] == "defect":
            ok = flagged if not s.get("expect_count") else codes.count("PARAM_OFF_SPEC") == s["expect_count"]
            rows.append({"name": s["name"], "kind": "defect", "caught": ok, "codes": codes})
        else:
            rows.append({"name": s["name"], "kind": "clean", "false_kill": flagged, "codes": codes})
    defects = [r for r in rows if r["kind"] == "defect"]
    cleans = [r for r in rows if r["kind"] == "clean"]
    caught = sum(1 for r in defects if r["caught"])
    false_kills = sum(1 for r in cleans if r["false_kill"])

    critic = _critic_eval()
    coverage = _power_coverage()

    go = (
        caught >= 2
        and false_kills == 0
        and coverage["coverage"] >= 0.8
        and not coverage["hardcode_gaps"]
        and (critic.get("skipped") or critic["caught"] >= 4)
    )
    summary = {
        "rows": rows,
        "defects_total": len(defects),
        "defects_caught": caught,
        "clean_total": len(cleans),
        "false_kills": false_kills,
        "critic": critic,
        "power_coverage": {k: v for k, v in coverage.items() if k != "rows"},
        "power_rows": coverage["rows"],
        "hardcode_gaps": coverage["hardcode_gaps"],
        "go": go,
    }
    for r in rows:
        if r["kind"] == "defect":
            print(f"[{'OK ' if r['caught'] else 'MISS'}] defect {r['name']}", flush=True)
        else:
            print(f"[{'OK ' if not r['false_kill'] else 'KILL'}] clean  {r['name']}", flush=True)
    print(
        f"== params eval: 错值拦截 {caught}/{len(defects)}, 负样本误杀 {false_kills}/{len(cleans)}, "
        f"电源块覆盖 {coverage['power_covered']}/{coverage['power_total']} = {coverage['coverage']:.0%}, "
        f"轨硬编码缺口 {len(coverage['hardcode_gaps'])}, "
        f"critic 捕获 {_critic_str(critic)} "
        f"-> {'Go' if go else 'NO-GO'} ==",
        flush=True,
    )
    Path("runs/params-eval-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return summary
