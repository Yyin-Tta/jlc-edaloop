"""P2-B 参数 sizing 规则引擎(ADR-0008 B 项子集)。

范围(刻意收窄,确定性优先):
  ①LED 限流电阻:R = (V_rail - V_f) / I_f,容值归 E24 系列;
  ②分压网络(反馈/检测):R_bottom/R_top 由目标比例定,总阻按偏置电流约束;
  ③LDO/BUCK 输入输出电容:按负载电流与纹波要求给典型值区间
    (datasheet 惯例值,非纹波全公式——那是 Phase 3)。

输出:SizingAdvice(公式代入过程+推荐值+依据),弱门禁定位——
  注入 planner 提示与交付文档,不阻断、不自动改连线。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

_E24 = [
    1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0,
    3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8, 7.5, 8.2, 9.1,
]


def _e24(value: float) -> tuple[float, str]:
    """归一到 E24 系列值,返回 (归一值, 描述)。"""
    if value <= 0:
        return value, "n/a"
    exp = math.floor(math.log10(value))
    dec = value / (10**exp)
    best = min(_E24, key=lambda s: abs(math.log(dec / s)))
    scaled = best * (10**exp)
    return scaled, f"E24"


def _fmt_ohm(r: float) -> str:
    if r >= 1e6:
        return f"{r / 1e6:g}M"
    if r >= 1e3:
        return f"{r / 1e3:g}k"
    return f"{r:g}"


@dataclass
class SizingAdvice:
    kind: str
    target: str
    formula: str
    result_raw: float
    result_rec: str
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"[sizing:{self.kind}@{self.target}] {self.formula} = {self.result_raw:.4g} → 推荐 {self.result_rec}"]
        lines += [f"    {n}" for n in self.notes]
        return "\n".join(lines)


def led_series_resistor(v_rail: float, v_forward: float, i_ma: float, *, target: str = "LED") -> SizingAdvice:
    """LED 限流:R = (V_rail - V_f)/I_f。"""
    i_a = i_ma / 1000.0
    r_raw = (v_rail - v_forward) / i_a if i_a > 0 else float("inf")
    r_e24, series = _e24(r_raw) if 0 < r_raw < 1e9 else (r_raw, "raw")
    p = (v_rail - v_forward) * i_a
    notes = [
        f"功率 P=(V-Vf)×I={p * 1000:.1f}mW → 0603(1/10W) 起步",
    ]
    if r_raw <= 0:
        notes.insert(0, "⚠ V_rail ≤ V_f:需要升压驱动或恒流源,串联电阻不可行")
    return SizingAdvice(
        kind="led-resistor",
        target=target,
        formula=f"({v_rail:g}V-{v_forward:g}V)/{i_ma:g}mA",
        result_raw=r_raw,
        result_rec=f"{_fmt_ohm(r_e24)}Ω ({series})",
        notes=notes,
    )


def divider_network(
    v_in: float, v_out: float, r_total: float, *, target: str = "divider"
) -> SizingAdvice:
    """分压:V_out = V_in × R_bot/(R_top+R_bot),总阻给定求两阻。"""
    if v_in <= 0 or not 0 < v_out < v_in or r_total <= 0:
        return SizingAdvice(
            kind="divider",
            target=target,
            formula="invalid input",
            result_raw=0,
            result_rec="n/a",
            notes=["⚠ 需 0 < V_out < V_in 且 R_total > 0"],
        )
    ratio = v_out / v_in
    r_bot = ratio * r_total
    r_top = r_total - r_bot
    rb24, _ = _e24(r_bot)
    rt24, _ = _e24(r_top)
    actual = rb24 / (rt24 + rb24) * v_in
    err = abs(actual - v_out) / v_out * 100
    return SizingAdvice(
        kind="divider",
        target=target,
        formula=f"Rbot={v_out:g}/{v_in:g}×{_fmt_ohm(r_total)}Ω",
        result_raw=r_bot,
        result_rec=f"Rtop={_fmt_ohm(rt24)}Ω / Rbot={_fmt_ohm(rb24)}Ω (E24)",
        notes=[
            f"E24 实际输出 {actual:.3f}V(误差 {err:.2f}%)",
            f"偏置电流 {v_in / r_total * 1e6:.1f}µA(总阻越小越稳、功耗越大)",
        ],
    )


_REG_CAPS = {
    "ldo": [
        ("C_IN", "10µF 钽/陶瓷 + 100nF 就近(AMS1117 数据手册惯例)"),
        ("C_OUT", "22µF + 100nF(LDO 依赖输出电容环路稳定,不可省)"),
    ],
    "buck": [
        ("C_IN", "22µF×2 低 ESR 陶瓷(开关纹波电流吸收)"),
        ("C_OUT", "22µF×2 + 按 ΔI_L 与允许纹波调整:C=ΔI_L/(8×f_sw×ΔV_ripple)"),
    ],
}


def reg_caps(reg_type: str, i_out_ma: float, *, target: str = "reg") -> SizingAdvice:
    """LDO/BUCK 输入输出电容:子集=datasheet 惯例区间(全纹波公式 Phase 3)。"""
    rows = _REG_CAPS.get(reg_type)
    if not rows:
        return SizingAdvice(
            kind="reg-caps", target=target, formula="unknown type", result_raw=0, result_rec="n/a", notes=["仅支持 ldo/buck"]
        )
    notes = [f"{name}: {desc}" for name, desc in rows]
    notes.append(f"负载 {i_out_ma:g}mA → LDO 压差功耗=(Vin-Vout)×I,注意散热铜")
    return SizingAdvice(
        kind="reg-caps",
        target=target,
        formula=f"{reg_type} @ {i_out_ma:g}mA",
        result_raw=i_out_ma,
        result_rec="见 notes(惯例值)",
        notes=notes,
    )


def size_for_plan(blocks: list[dict]) -> list[SizingAdvice]:
    """BlockPlan.blocks(同构 dict)→ 可确定的 sizing 建议集。

    识别:ports_binding 里的电压轨(3V3/5V)+块类别(led/divider 由 block_id 启发)。
    这一层保守:识别不出的不猜。
    """
    advices: list[SizingAdvice] = []
    rails: dict[str, float] = {}
    for b in blocks:
        for net in (b.get("ports_binding") or {}).values():
            n = str(net).upper().lstrip("+")
            if n == "3V3" or n == "3.3V":
                rails.setdefault("3V3", 3.3)
            elif n == "5V":
                rails.setdefault("5V", 5.0)
            elif n == "12V":
                rails.setdefault("12V", 12.0)
    for b in blocks:
        bid = (b.get("block_id") or "").lower()
        inst = b.get("instance") or bid
        if "led" in bid:
            rail = 3.3 if "3V3" in str(b.get("ports_binding", {})).upper() else (5.0 if "5V" in str(b.get("ports_binding", {})).upper() else 3.3)
            advices.append(led_series_resistor(rail, 2.0, 5, target=inst))
        if "ldo" in bid or "ams1117" in bid:
            advices.append(reg_caps("ldo", 800, target=inst))
        if "buck" in bid or "mp1584" in bid or "tps5430" in bid or "sy8089" in bid:
            advices.append(reg_caps("buck", 2000, target=inst))
        if "tl431" in bid:
            advices.append(divider_network(rails.get("5V", 5.0), 3.0, 10000, target=inst))
    return advices
