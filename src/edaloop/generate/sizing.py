"""P2-B/P4-4① 参数 sizing 规则引擎(ADR-0008 B 项子集 → P4-4 输入来源表升级)。

范围(刻意收窄,确定性优先):
  ①LED 限流电阻:R = (V_rail - V_f) / I_f,容值归 E24 系列;
  ②分压网络(反馈/检测):R_bottom/R_top 由目标比例定,总阻按偏置电流约束;
  ③LDO/BUCK 输入输出电容:按负载电流与纹波要求给典型值区间
    (datasheet 惯例值,非纹波全公式——那是 Phase 3)。

P4-4① 输入来源表(每条公式的每个输入都要有出处,SizingAdvice.inputs):
  - 轨相关输入**零硬编码**:电压一律 IR rail 优先(含宽压区间),网名家族兜底
    (与 check_voltage_compat 同一张惯例表);两者都无 → 该公式降级不猜。
  - Vf/If/f_sw/Rja/Vref 走 P4-0 器件参数槽(BlockRecord.electrical.params);
    槽缺数据时用**具名工程缺省**(出处标 ENGINEERING-DEFAULT,回填后自动覆盖)。
  - 工程系数白名单 _COEFFS(系数≠硬编码:具名、集中、可引用)。

输出:弱门禁定位——注入 planner 提示与交付文档,不阻断、不自动改连线。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

_E24 = [
    1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0,
    3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8, 7.5, 8.2, 9.1,
]

# ---- 工程系数白名单(系数≠硬编码:具名、集中、可引用;改动需过评审) ----
_COEFFS = {
    "TVS_VRWM_MARGIN": 1.1,   # VRWM ≥ 1.1×V_rail(防误触发)
    "TVS_VC_RATIO": 2.0,      # VC ≤ 2×V_rail(钳位低于后级耐压的工程上界)
    "PPTC_DERATE": 1.25,      # I_hold ≥ 1.25×I_load(防浪涌误动作)
    "BUCK_DI_RATIO": 0.3,     # ΔI_L = 0.3×I_out(纹波电流惯例)
}

# ---- 参数槽缺数据时的具名工程缺省(出处可追;槽回填后自动被覆盖) ----
_ENGINEERING_DEFAULTS = {
    "led_vf_v": ("2.0", "LED 通用红光典型 Vf(槽 vf 缺)"),
    "led_if_ma": ("5", "指示灯惯例 5mA(槽 if 缺)"),
    "rja_c_w": ("65", "SOT-223 典型 Rja(槽 rja 缺)"),
    "f_sw_buck_khz": ("500", "buck 开关频率典型(槽 f_sw 缺)"),
    "f_sw_boost_khz": ("1200", "boost 开关频率典型(槽 f_sw 缺)"),
    "reg_iout_ma": ("800", "电源块输出电流典型(槽 i_max/i_typ 缺)"),
    "entry_i_ma": ("120", "输入口负载电流典型(轨 imax 缺)"),
    "tl431_vref_v": ("2.495", "TL431 基准 Vref(槽 vref 缺)"),
    "tl431_rtotal": ("10000", "分压总阻 10k(偏置电流取舍)"),
    "t_amb_c": ("25", "室温缺省 25°C"),
    "t_j_max_c": ("105", "结温上限惯例 105°C"),
}


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


def _fmt_uh(v: float) -> str:
    if v >= 1e6:
        return f"{v / 1e6:g}M"
    if v >= 1e3:
        return f"{v / 1e3:g}k"
    return f"{v:g}µ"


def _fmt_cap(f: float) -> str:
    """电容(法拉)→ 标准件表键口径:22µF→'22u'、100nF→'100n'。"""
    if f >= 1e-6:
        return f"{f / 1e-6:g}u"
    if f >= 1e-9:
        return f"{f / 1e-9:g}n"
    return f"{f / 1e-12:g}p"


def _eng_num(text: str) -> float | None:
    """"1.5MHz"/"500k"/"150kHz"/"2.495" → 数值(k=1e3, M=1e6, m=1e-3;Hz 后缀剥掉)。

    大小写敏感:M=兆、m=毫(剥 Hz 后缀时保留原大小写,"1.5MHz"→"1.5M"→1.5e6)。
    """
    t = str(text).strip().replace("Hz", "").replace("HZ", "").replace("hz", "")
    m = re.fullmatch(r"([\d.]+)\s*([kKmM])?", t)
    if not m:
        try:
            return float(t)
        except ValueError:
            return None
    v = float(m.group(1))
    s = m.group(2) or ""
    if s in ("k", "K"):
        return v * 1e3
    if s == "M":
        return v * 1e6
    if s == "m":
        return v * 1e-3
    return v


@dataclass
class SizingAdvice:
    kind: str
    target: str
    formula: str
    result_raw: float
    result_rec: str
    notes: list[str] = field(default_factory=list)
    # P4-4① 输入来源表:(输入名, 值文本, 出处) —— 轨输入出处必须是 IR rail/家族兜底之一
    inputs: list[tuple[str, str, str]] = field(default_factory=list)
    # P4-4③ PARAM_OFF_SPEC 消费:推荐值的机读表示(kind 对齐标准件表键口径)
    rec_value: str = ""
    rec_kind: str = ""  # resistance|capacitance|inductance|v_rating|i_rating
    nets: list[str] = field(default_factory=list)  # 推荐元件跨接的网对(拓扑无歧义时才给)

    def render(self) -> str:
        lines = [f"[sizing:{self.kind}@{self.target}] {self.formula} = {self.result_raw:.4g} → 推荐 {self.result_rec}"]
        for name, val, src in self.inputs:
            lines.append(f"    输入 {name}={val} ← {src}")
        lines += [f"    {n}" for n in self.notes]
        return "\n".join(lines)

    def gap(self) -> str | None:
        """输入来源缺口(轨输入无出处=硬编码嫌疑,必须有);无缺口返回 None。"""
        rail_inputs = [i for i in self.inputs if i[0].startswith(("V_", "v_", "I_load", "轨"))]
        missing = [i[0] for i in rail_inputs if not i[2]]
        return ", ".join(missing) if missing else None


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
    di = _COEFFS["BUCK_DI_RATIO"] * i_out
    if l_uh is None:
        l_uh = (v_out * (1 - d)) / (di * f_sw_khz * 1000) * 1e6
    di_actual = (v_out * (1 - d)) / (l_uh * 1e-6 * f_sw_khz * 1000)
    c_out = di_actual / (8 * f_sw_khz * 1000 * delta_v_mv / 1000)
    l24, _ = _e24(l_uh)
    notes = [
        f"D={d:.3f};ΔI_L={di_actual * 1000:.0f}mA(目标 {_COEFFS['BUCK_DI_RATIO']:g}×I_out)",
        f"输出电容 C=ΔI/(8·f·ΔV)={c_out * 1e6:.0f}µF@ΔV={delta_v_mv:g}mV(取 Low-ESR 陶瓷,并联均摊)",
        f"电感饱和电流 ≥ I_out+ΔI/2={i_out + di_actual / 2:.2f}A;DCR 越小效率越高",
    ]
    return SizingAdvice(
        kind="buck-ripple",
        target=target,
        formula=f"L=Vo(1-D)/(ΔI·f), ΔI={_COEFFS['BUCK_DI_RATIO']:g}×{i_out_ma:g}mA",
        result_raw=l_uh,
        result_rec=f"L={_fmt_uh(l24)}H(E24) + C_out≈{c_out * 1e6:.0f}µF",
        notes=notes,
    )


def tvs_rating(v_rail: float, *, target: str = "input") -> SizingAdvice:
    """TVS 规格:VRWM ≥ 1.1×V_rail;VC(钳位) < 被保护器件耐压;功率按浪涌估算(P3-3 子集:给关键两值)。"""
    vrwm = v_rail * _COEFFS["TVS_VRWM_MARGIN"]
    vc_max = v_rail * _COEFFS["TVS_VC_RATIO"]
    return SizingAdvice(
        kind="tvs-rating",
        target=target,
        formula=f"VRWM≥{_COEFFS['TVS_VRWM_MARGIN']:g}×{v_rail:g}V;VC<{_COEFFS['TVS_VC_RATIO']:g}×V_rail",
        result_raw=vrwm,
        result_rec=f"VRWM≥{vrwm:.1f}V / VC≤{vc_max:.0f}V(SMAJ/SMBJ 系列按此筛)",
        notes=[
            "VRWM 高于工作电压防误触发;VC 低于后级最大耐压才有效",
            "功率档:600W(SMAJ)/400W(SMBJ 视浪涌等级;车载选 SMBJ 以上",
        ],
    )


def fuse_rating(i_load_ma: float, *, derate: float | None = None, target: str = "input") -> SizingAdvice:
    """保险丝/PPTC 规格:I_hold ≥ 1.25×I_load(防误熔),I_trip < 走线长期承受。"""
    if derate is None:
        derate = _COEFFS["PPTC_DERATE"]
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


# ---------- P4-4① 轨来源解析(IR rail 优先,家族兜底;零硬编码) ----------


def rail_volts_map(ir) -> dict[str, tuple[float, float, float | None, str]]:
    """IR 权威轨表:归一轨名 → (v_lo, v_hi, imax_A|None, 出处文本)。

    电压区间宽压语义与 check_rails/_rail_span 同源;无电压信息的轨不进表(公式不猜)。
    """
    from edaloop.validate.checks import _family_volts, _rail_family, _rail_span, norm_rail

    out: dict[str, tuple[float, float, float | None, str]] = {}
    if ir is None:
        return out
    for rail in getattr(getattr(ir, "power", None), "rails", None) or []:
        lo, hi = _rail_span(rail)
        src = f"IR rail {rail.name}={rail.v_text()}"
        if lo is None:
            v = _family_volts(_rail_family(rail.name or ""))
            if v is None:
                continue
            lo = hi = v
            src += "(家族名兜底)"
        imax = getattr(rail, "imax", None)
        if imax is not None:
            src += f", imax={imax:g}A"
        out[norm_rail(rail.name or "")] = (lo, hi, imax, src)
    return out


def net_volts(net: str, rails: dict[str, tuple[float, float, float | None, str]]) -> tuple[float | None, str]:
    """网名 → (电压, 出处):IR rail 精确命中,否则网名家族兜底(与电压检查器同表)。"""
    from edaloop.validate.checks import _family_volts, _rail_family, norm_rail

    n = norm_rail(str(net))
    if n in rails:
        lo, hi, _, src = rails[n]
        v = hi if hi is not None else lo
        return v, src
    fam = _family_volts(_rail_family(str(net)))
    if fam is not None:
        return fam, f"网名家族兜底 {net}→{fam:g}V(惯例表,与 check_voltage_compat 同源)"
    return None, ""


def _attr(b, name, default=None):
    if isinstance(b, dict):
        return b.get(name, default)
    return getattr(b, name, default)


def _elec_of(b, catalog: dict | None):
    rec = (catalog or {}).get(_attr(b, "block_id"))
    return getattr(rec, "electrical", None) if rec is not None else None


def _slot(elec, key: str, default_key: str, *, unit="") -> tuple[float | None, str]:
    """器件参数槽优先,缺则具名工程缺省;返回 (数值, 出处)。"""
    raw = (getattr(elec, "params", None) or {}).get(key, "")
    if raw:
        v = _eng_num(raw)
        if v is not None:
            return v, f"参数槽 {key}={raw}{unit}(槽出处见 electrical.source)"
    dv, dsrc = _ENGINEERING_DEFAULTS[default_key]
    return float(dv), f"ENGINEERING-DEFAULT {dsrc}"


def _subkind(b, catalog: dict | None) -> str:
    """块 → sizing 子类(block_id + upstream.id 关键词;识别不出的返回 '')。"""
    rec = (catalog or {}).get(_attr(b, "block_id"))
    uid = (rec.upstream.id if rec is not None and rec.upstream else "") or ""
    text = f"{_attr(b, 'block_id') or ''} {uid}".lower()
    if "led" in text:
        return "led"
    if "tl431" in text:
        return "tl431"
    if "vehicle" in text or "wide-input" in text or "tps54360" in text:
        return "vehicle-entry"
    if "usb-c" in text or "usbc" in text:
        return "usb-entry"
    if "buckboost" in text or "tps63802" in text:
        return "buck"  # 开关稳压按 buck 惯例给纹波/电容建议(含降级注记)
    if "boost" in text or "mt3608" in text or "sy7088" in text:
        return "boost"
    if "buck" in text or "mp1584" in text or "tps5430" in text or "sy8089" in text or "xl1509" in text or "mp2359" in text:
        return "buck"
    if "ldo" in text or "ams1117" in text or "rt9193" in text or "1117" in text:
        return "ldo"
    if "charger" in text or "tp4056" in text or "lgs4056" in text or "bq24074" in text:
        return "charger"
    if "isolated" in text or "b0505s" in text:
        return "isolated-dc"
    if "softstart" in text or "highside" in text:
        return "power-switch"
    if "terminal" in text and "5v" in text:
        return "terminal-5v"
    return ""


_GND_TOKENS = ("GND", "AGND", "DGND", "VSS")


def _io_nets(b) -> tuple[list[str], list[str]]:
    """块绑定网拆 (输入侧, 输出侧):GND 口不进;VIN/VBUS/VCC 口算输入侧,
    轨名口/VOUT/VSYS 口算输出侧(电源类块轨名口是输出侧,与 _PWR_IN_POWER_CAT 同义)。"""
    ins: list[str] = []
    outs: list[str] = []
    pb = _attr(b, "ports_binding") or {}
    for port, net in (pb.items() if isinstance(pb, dict) else []):
        n = str(net)
        p = str(port).upper()
        if any(t in p for t in _GND_TOKENS) or any(t in n.upper() for t in _GND_TOKENS):
            continue
        if any(t in p for t in ("VIN", "VBUS", "VCC")):
            ins.append(n)
        elif any(t in p for t in ("VOUT", "OUT", "VBATT", "BAT", "SW", "VSYS", "3V3", "5V", "1V8", "1V2")):
            outs.append(n)
        else:
            ins.append(n)  # 信号口(如 LED 的 DRV)归输入侧:驱动网
    return ins, outs


def _rail_imax_for(net: str, rails) -> tuple[float | None, str]:
    from edaloop.validate.checks import norm_rail

    n = norm_rail(str(net))
    if n in rails and rails[n][2] is not None:
        return rails[n][2], f"IR rail {n} imax={rails[n][2]:g}A"
    return None, ""


def size_for_plan(blocks: list, ir=None, catalog: dict | None = None) -> list[SizingAdvice]:
    """plan.blocks(PlannedBlock 或同构 dict)→ 可确定的 sizing 建议集。

    P4-4①:轨输入零硬编码——ir 给出 IR 权威轨表;ir 缺省时仅剩网名家族兜底
    (兜底猜不出的网 → 该公式降级不猜,不进建议)。识别不出的块不猜。
    """
    advices: list[SizingAdvice] = []
    rails = rail_volts_map(ir)
    for b in blocks:
        bid = str(_attr(b, "block_id") or "")
        inst = str(_attr(b, "instance") or bid)
        kind = _subkind(b, catalog)
        elec = _elec_of(b, catalog)
        ins, outs = _io_nets(b)

        def _v(net: str) -> tuple[float | None, str]:
            v, src = net_volts(net, rails)
            if v is None and len(rails) == 1:
                lo, hi, _, rsrc = next(iter(rails.values()))
                return (hi if hi is not None else lo), f"IR 单轨推断({rsrc})"
            return v, src

        if kind == "led":
            sig = [n for n in ins + outs if n] or list((_attr(b, "ports_binding") or {}).values())
            drive = sig[0] if sig else ""
            v, vsrc = _v(drive) if drive else (None, "")
            vf, vfsrc = _slot(elec, "vf", "led_vf_v", unit="V")
            ifma, ifsrc = _slot(elec, "if", "led_if_ma", unit="mA")
            if v is None:
                advices.append(SizingAdvice(
                    kind="led-resistor", target=inst, formula="(轨电压未知)",
                    result_raw=0, result_rec="n/a",
                    notes=[f"⚠ 限流电阻无法确定:驱动网 {drive or '?'} 不是声明轨且家族兜底不出(不猜)"],
                    inputs=[("V_rail", "?", "")],
                ))
                continue
            a = led_series_resistor(v, vf, ifma, target=inst)
            r_e24, _ = _e24(a.result_raw) if a.result_raw > 0 else (a.result_raw, "")
            a.inputs = [("V_rail", f"{v:g}V", vsrc), ("V_f", f"{vf:g}V", vfsrc), ("I_f", f"{ifma:g}mA", ifsrc)]
            a.rec_value, a.rec_kind = _fmt_ohm(r_e24), "resistance"
            # 限流电阻必触驱动节点(串联件,另一端是内部节点)→ nets 给驱动网单网,
            # PARAM_OFF_SPEC 通道 B 用「恰有一网相交」匹配该节点上的 std 电阻。
            if drive:
                a.nets = [drive]
            advices.append(a)

        elif kind in ("ldo",):
            vin, vinsrc = None, ""
            for n in ins:
                v, s = _v(n)
                if v is not None:
                    vin, vinsrc = v, s
                    break
            vout, voutsrc = (None, "")
            for n in outs:
                v, s = _v(n)
                if v is not None:
                    vout, voutsrc = v, s
                    break
            imax = getattr(elec, "i_max", None) if elec is not None else None
            ityp = getattr(elec, "i_typ", None) if elec is not None else None
            if imax is not None:
                i_ma, isrc = imax * 1000, f"参数槽 electrical.i_max={imax:g}A({getattr(elec, 'source', '') or '库回填'})"
            elif ityp is not None:
                i_ma, isrc = ityp * 1000, f"参数槽 electrical.i_typ={ityp:g}A"
            else:
                dv, dsrc = _ENGINEERING_DEFAULTS["reg_iout_ma"]
                i_ma, isrc = float(dv), f"ENGINEERING-DEFAULT {dsrc}"
            ca = reg_caps("ldo", i_ma, target=inst)
            ca.inputs = [("I_out", f"{i_ma:g}mA", isrc)]
            advices.append(ca)
            if vin is not None and vout is not None and vout < vin:
                rja, rjasrc = _slot(elec, "rja", "rja_c_w", unit="°C/W")
                tamb, tambsrc = (float(_ENGINEERING_DEFAULTS["t_amb_c"][0]), "ENGINEERING-DEFAULT " + _ENGINEERING_DEFAULTS["t_amb_c"][1])
                tjmax = float(_ENGINEERING_DEFAULTS["t_j_max_c"][0])
                p = (vin - vout) * i_ma / 1000
                th = thermal_check(p, rja, tamb, t_max=tjmax, target=inst)
                th.inputs = [
                    ("V_in", f"{vin:g}V", vinsrc), ("V_out", f"{vout:g}V", voutsrc),
                    ("I_out", f"{i_ma:g}mA", isrc), ("R_ja", f"{rja:g}°C/W", rjasrc),
                    ("T_amb", f"{tamb:g}°C", tambsrc),
                ]
                advices.append(th)

        elif kind in ("buck", "boost"):
            is_boost = kind == "boost"
            vin, vinsrc = (None, "")
            for n in ins:
                v, s = _v(n)
                if v is not None:
                    vin, vinsrc = v, s
                    break
            vout, voutsrc = (None, "")
            for n in outs:
                v, s = _v(n)
                if v is not None:
                    vout, voutsrc = v, s
                    break
            imax = getattr(elec, "i_max", None) if elec is not None else None
            if imax is not None:
                i_ma, isrc = imax * 1000, f"参数槽 electrical.i_max={imax:g}A({getattr(elec, 'source', '') or '库回填'})"
            else:
                rail_i, rail_isrc = (None, "")
                for n in outs:
                    rail_i, rail_isrc = _rail_imax_for(n, rails)
                    if rail_i is not None:
                        break
                if rail_i is not None:
                    i_ma, isrc = rail_i * 1000, rail_isrc
                else:
                    dv, dsrc = _ENGINEERING_DEFAULTS["reg_iout_ma"]
                    i_ma, isrc = float(dv), f"ENGINEERING-DEFAULT {dsrc}"
            fsw, fswsrc = _slot(elec, "f_sw", "f_sw_boost_khz" if is_boost else "f_sw_buck_khz", unit="Hz")
            fsw_khz = fsw / 1000.0 if fsw >= 1000 else fsw  # 槽给 Hz、缺省给 kHz,统一到 kHz
            ca = reg_caps("buck", i_ma, target=inst)
            ca.inputs = [("I_out", f"{i_ma:g}mA", isrc)]
            advices.append(ca)
            if vin is not None and vout is not None and (vout < vin if not is_boost else vin < vout):
                a = buck_ripple(vout, vin, i_ma, fsw_khz, target=inst)
                a.inputs = [
                    ("V_in", f"{vin:g}V", vinsrc), ("V_out", f"{vout:g}V", voutsrc),
                    ("I_out", f"{i_ma:g}mA", isrc), ("f_sw", f"{fsw_khz:g}kHz", fswsrc),
                ]
                if is_boost:
                    a.notes.append("⚠ boost 拓扑套 buck 纹波公式为近似(升压电感按输入侧电流取)")
                out_net = outs[0] if outs else ""
                cout_f = _parse_cout(a)
                if cout_f and out_net:
                    a.rec_value, a.rec_kind = _fmt_cap(_e24(cout_f)[0]), "capacitance"
                    a.nets = [out_net, "GND"]
                advices.append(a)
            elif vin is None or vout is None:
                advices.append(SizingAdvice(
                    kind="buck-ripple", target=inst, formula="(轨电压未知)",
                    result_raw=0, result_rec="n/a",
                    notes=["⚠ 纹波公式降级:输入/输出网不在声明轨且家族兜底不出(不猜)"],
                    inputs=[("V_in" if vin is None else "V_out", "?", "")],
                ))

        elif kind == "tl431":
            from edaloop.validate.checks import norm_rail

            cands = (ins + outs) or list((_attr(b, "ports_binding") or {}).values())
            # 被监控轨 = 第一个「轨/家族可解」的网(信号口如 ALARM 是开漏输出,不是监控对象;
            # 不用 _v 的单轨推断选网——单轨下信号网也会被推断出电压,选网会选错对象)
            mon_net = next(
                (n for n in cands if n and net_volts(n, rails)[0] is not None),
                cands[0] if cands else "",
            )
            v_hi, vsrc = _v(mon_net) if mon_net else (None, "")
            # 跳变阈值 = 被监控轨的 v_min(低压拐点语义);分压公式:tap=Vref 时 V_bat=V_trip,
            # 即 Rbot/Rtot = Vref/V_trip —— divider_network(v_in=V_trip, v_out=Vref) 同比。
            lo = rails.get(norm_rail(mon_net), (None,))[0] if mon_net else None
            vref, vrefsrc = _slot(elec, "vref", "tl431_vref_v", unit="V")
            rtot, rtotsrc = (float(_ENGINEERING_DEFAULTS["tl431_rtotal"][0]), "ENGINEERING-DEFAULT " + _ENGINEERING_DEFAULTS["tl431_rtotal"][1])
            if v_hi is not None and lo is not None and lo < v_hi and 0 < vref < lo:
                a = divider_network(lo, vref, rtot, target=inst)
                a.inputs = [
                    ("V_trip", f"{lo:g}V", f"IR rail {mon_net} v_min(低压拐点=跳变阈值)"),
                    ("V_in(轨上限)", f"{v_hi:g}V", vsrc),
                    ("V_ref", f"{vref:g}V", vrefsrc), ("R_total", f"{_fmt_ohm(rtot)}Ω", rtotsrc),
                ]
                advices.append(a)
            else:
                advices.append(SizingAdvice(
                    kind="divider", target=inst, formula="(监控轨未知)",
                    result_raw=0, result_rec="n/a",
                    notes=["⚠ TL431 分压降级:监控网无 IR 轨区间(v_min 缺,不猜)"],
                    inputs=[("V_in", "?", "")],
                ))

        elif kind in ("vehicle-entry", "usb-entry", "terminal-5v", "charger", "isolated-dc", "power-switch"):
            v, vsrc = (None, "")
            for n in ins:
                v, vsrc = _v(n)
                if v is not None:
                    break
            if v is not None:
                ta = tvs_rating(v, target=inst)
                ta.inputs = [("V_rail", f"{v:g}V", vsrc),
                             ("系数 TVS_VRWM_MARGIN", f"{_COEFFS['TVS_VRWM_MARGIN']:g}", "工程系数白名单 _COEFFS")]
                advices.append(ta)
            rail_i, rail_isrc = (None, "")
            for n in ins:
                rail_i, rail_isrc = _rail_imax_for(n, rails)
                if rail_i is not None:
                    break
            imax = getattr(elec, "i_max", None) if elec is not None else None
            if rail_i is not None:
                i_ma, isrc = rail_i * 1000, rail_isrc
            elif imax is not None:
                i_ma, isrc = imax * 1000, f"参数槽 electrical.i_max={imax:g}A"
            else:
                dv, dsrc = _ENGINEERING_DEFAULTS["entry_i_ma"]
                i_ma, isrc = float(dv), f"ENGINEERING-DEFAULT {dsrc}"
            fa = fuse_rating(i_ma, target=inst)
            fa.inputs = [("I_load", f"{i_ma:g}mA", isrc),
                         ("系数 PPTC_DERATE", f"{_COEFFS['PPTC_DERATE']:g}", "工程系数白名单 _COEFFS")]
            advices.append(fa)
            if kind == "isolated-dc":
                ca = reg_caps("buck", i_ma, target=inst)
                ca.inputs = [("I_out", f"{i_ma:g}mA", isrc)]
                ca.notes.append("⚠ 隔离 DC-DC 模块:按开关惯例值给,以模块 datasheet 为准")
                advices.append(ca)
    return advices


def _parse_cout(a: SizingAdvice) -> float | None:
    """buck_ripple 建议文本里解回输出电容法拉值(notes[1] 'C=…µF@…')。"""
    import re as _re

    for n in a.notes:
        m = _re.search(r"([0-9.]+)µF", n)
        if m:
            return float(m.group(1)) * 1e-6
    return None
