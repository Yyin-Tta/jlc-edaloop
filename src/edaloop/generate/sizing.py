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


def buck_ripple(
    v_out: float,
    v_in: float,
    i_out_ma: float,
    f_sw_khz: float,
    *,
    l_uh: float | None = None,
    delta_v_mv: float = 30.0,
    target: str = "buck",
) -> SizingAdvice:
    """BUCK 纹波全公式(P3-3):

    电感: ΔI_L = V_out×(1-D)/(L×f), D=V_out/V_in;按 ΔI=0.3×I_out 反解 L
    输出电容: C = ΔI_L/(8×f×ΔV_ripple)
    """
    i_out = i_out_ma / 1000.0
    d = v_out / v_in if v_in > 0 else 0
    if i_out <= 0 or not 0 < d < 1:
        return SizingAdvice(
            kind="buck-ripple", target=target, formula="invalid", result_raw=0,
            result_rec="n/a", notes=["⚠ 需 0<Vout<Vin 且 Iout>0"],
        )
    di = 0.3 * i_out
    if l_uh is None:
        l_uh = (v_out * (1 - d)) / (di * f_sw_khz * 1000) * 1e6
    di_actual = (v_out * (1 - d)) / (l_uh * 1e-6 * f_sw_khz * 1000)
    c_out = di_actual / (8 * f_sw_khz * 1000 * delta_v_mv / 1000)
    l24, _ = _e24(l_uh)
    notes = [
        f"D={d:.3f};ΔI_L={di_actual * 1000:.0f}mA(目标 30% I_out)",
        f"输出电容 C=ΔI/(8·f·ΔV)={c_out * 1e6:.0f}µF@ΔV={delta_v_mv:g}mV(取 Low-ESR 陶瓷,并联均摊)",
        f"电感饱和电流 ≥ I_out+ΔI/2={i_out + di_actual / 2:.2f}A;DCR 越小效率越高",
    ]
    return SizingAdvice(
        kind="buck-ripple",
        target=target,
        formula=f"L=Vo(1-D)/(ΔI·f), ΔI=0.3×{i_out_ma:g}mA",
        result_raw=l_uh,
        result_rec=f"L={_fmt_uh(l24)}H(E24) + C_out≈{c_out * 1e6:.0f}µF",
        notes=notes,
    )


def _fmt_uh(v: float) -> str:
    if v >= 1e6:
        return f"{v / 1e6:g}M"
    if v >= 1e3:
        return f"{v / 1e3:g}k"
    return f"{v:g}µ"


def tvs_rating(v_rail: float, *, target: str = "input") -> SizingAdvice:
    """TVS 规格:VRWM ≥ 1.1×V_rail;VC(钳位) < 被保护器件耐压;功率按浪涌估算(P3-3 子集:给关键两值)。"""
    vrwm = v_rail * 1.1
    vc_max = v_rail * 2.0
    return SizingAdvice(
        kind="tvs-rating",
        target=target,
        formula=f"VRWM≥1.1×{v_rail:g}V;VC<2×V_rail",
        result_raw=vrwm,
        result_rec=f"VRWM≥{vrwm:.1f}V / VC≤{vc_max:.0f}V(SMAJ/SMBJ 系列按此筛)",
        notes=[
            "VRWM 高于工作电压防误触发;VC 低于后级最大耐压才有效",
            "功率档:600W(SMAJ)/400W(SMBJ 视浪涌等级;车载选 SMBJ 以上",
        ],
    )


def fuse_rating(i_load_ma: float, *, derate: float = 1.25, target: str = "input") -> SizingAdvice:
    """保险丝/PPTC 规格:I_hold ≥ 1.25×I_load(防误熔),I_trip < 走线长期承受。"""
    i_hold = i_load_ma * derate / 1000
    return SizingAdvice(
        kind="fuse-rating",
        target=target,
        formula=f"I_hold≥{derate:g}×{i_load_ma:g}mA",
        result_raw=i_hold,
        result_rec=f"I_hold≥{i_hold:.2f}A(自恢复 PPTC 按此档)",
        notes=["降额 25% 防浪涌误动作;PPTC 恢复后保持电流会随温度下降,高温环境再降额"],
    )


def thermal_check(power_w: float, r_ja: float, t_amb: float = 25.0, *, t_max: float = 105.0, target: str = "reg") -> SizingAdvice:
    """热校核:T_j = T_amb + P×R_ja;结温裕度提示(P3-3:提示层,不判死)。"""
    t_j = t_amb + power_w * r_ja
    margin = t_max - t_j
    notes = [
        f"估算结温 {t_j:.0f}°C(上限 {t_max:g}°C,裕量 {margin:.0f}°C)",
        "裕量 <20°C:加铺铜/散热过孔或换封装;R_ja 取决于铺铜面积, datasheet 典型值为最小铺铜条件",
    ]
    if margin < 20:
        notes.insert(0, "⚠ 热裕量不足,建议加大铜箔或改封装")
    return SizingAdvice(
        kind="thermal",
        target=target,
        formula=f"Tj={t_amb:g}+{power_w:g}W×{r_ja:g}°C/W",
        result_raw=t_j,
        result_rec=f"Tj≈{t_j:.0f}°C(裕量{margin:.0f}°C)",
        notes=notes,
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
            advices.append(thermal_check(1.7 * 0.5, 60.0, target=inst))
        if "buck" in bid or "mp1584" in bid or "tps5430" in bid or "sy8089" in bid or "xl1509" in bid or "mp2359" in bid:
            advices.append(reg_caps("buck", 2000, target=inst))
            advices.append(buck_ripple(3.3, 12.0, 1000, 500, target=inst))
        if "boost" in bid or "mt3608" in bid or "sy7088" in bid:
            advices.append(buck_ripple(5.0, 3.7, 1000, 1200, target=inst))
        if "tl431" in bid:
            advices.append(divider_network(rails.get("5V", 5.0), 3.0, 10000, target=inst))
        if "vehicle" in bid or "wide-input" in bid or "tps54360" in bid:
            advices.append(tvs_rating(24.0, target=inst))
            advices.append(fuse_rating(120, target=inst))
        if "usb-c" in bid or "usbc" in bid:
            advices.append(tvs_rating(5.0, target=inst))
            advices.append(fuse_rating(2000, target=inst))
    return advices
