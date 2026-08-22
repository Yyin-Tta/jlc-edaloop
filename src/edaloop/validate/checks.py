from __future__ import annotations

import re

from edaloop.generate.models import BlockPlan
from edaloop.intent.ir import DesignIR
from edaloop.validate.models import Finding, Where

_RAIL_ALIASES = {
    "3v3": "3V3",
    "3.3v": "3V3",
    "3.3": "3V3",
    "+3v3": "3V3",
    "+3.3v": "3V3",
    "5v": "5V",
    "+5v": "5V",
    "5.0": "5V",
    "12v": "12V",
    "+12v": "12V",
    "24v": "24V",
    "gnd": "GND",
    "vbus": "VBUS",
    "vbat": "VBAT",
    "bat": "VBAT",
    # 电池焊盘惯用名(锂电保护自由拓扑 planner 实测命名,req-08):B+/BAT+ 即 VBAT 家族
    "b+": "VBAT",
    "bat+": "VBAT",
    "b-": "GND",
    "bat-": "GND",
}


def norm_rail(name: str) -> str:
    n = name.strip().lower().replace(" ", "")
    return _RAIL_ALIASES.get(n, name.strip().upper())


_ISO_RE = re.compile(r"(_?ISO|_?VIS[O_]|\+VO\b)", re.IGNORECASE)
_VOLT_WORDS = {"VIS": "5", "VO": "5", "VISO": "5", "VBAT": "3.7", "BAT": "3.7"}


def _norm_volts(key: str) -> str:
    """数值归一:'3'=='3.3'(3V3 惯例),'3.0'=='3'。"""
    try:
        f = float(key)
    except ValueError:
        return key
    if abs(f - 3.3) < 0.05:
        return "3.3"
    return f"{f:g}"


def _rail_family(name: str) -> str:
    """轨名归一到家族(5V_ISO/VISO/+VO → '5|iso');隔离侧惯用名按 5V。

    先过 _RAIL_ALIASES(小写紧凑键)——电池焊盘 B+/BAT+ 等惯用名经此并入 VBAT 家族。
    """
    n = _RAIL_ALIASES.get(name.strip().lower().replace(" ", ""), name.strip().upper())
    iso = bool(_ISO_RE.search(n))
    base = n
    for strip_re in (r"^[\+\-]", r"_?ISO\w*$", r"^VIS[O_]_?", r"^\+VO_?"):
        base = re.sub(strip_re, "", base)
    m3 = re.fullmatch(r"(\d)V(\d)", base)
    if m3:
        key = f"{m3.group(1)}.{m3.group(2)}"
    else:
        volts = re.search(r"(\d+(?:\.\d+)?)\s*V", base)
        if volts:
            key = volts.group(1)
        else:
            word = base.rstrip("_") or n
            key = _VOLT_WORDS.get(word, _VOLT_WORDS.get(n, word))
    return f"{_norm_volts(key)}|{'iso' if iso else 'main'}"


# ---------- P4-3 电气门禁:电压兼容 + 电流预算(强/弱分级,三态降级) ----------

_GND_FAMILY = _rail_family("GND")
# 输出侧/充电电池侧端口名(稳压器 VOUT、充电器 BAT/VBATT、电源路径 VSYS)——按负载核对会误杀
_PWR_OUT_TOKENS = ("VOUT", "OUT", "VBATT", "BAT", "SW")
# power 类块(cat=power)只认这些输入名:3V3/+5V/VSYS 等轨名在电源类块上是输出侧
# (seeds 实证:sy8089 的输出端口就叫 "3V3",xl1509 的叫 "+5V",bq24074 的 "VSYS"/"VBATT")
_PWR_IN_POWER_CAT = ("VIN", "VCC", "VBUS")


def _family_volts(family: str) -> float | None:
    """家族键里的电压值('3.3|main'→3.3);解析不出(如 'VIN_RAW|main')→ None=UNKNOWN。"""
    try:
        return float(family.split("|")[0])
    except ValueError:
        return None


def _rail_span(rail) -> tuple[float | None, float | None]:
    """轨电压区间:范围优先(voltage 与范围并存时取范围,§9.4 宽压语义);只有标称→(v,v)。"""
    if rail.v_min is not None or rail.v_max is not None:
        return rail.v_min, rail.v_max
    if rail.voltage is not None:
        return rail.voltage, rail.voltage
    return None, None


def _ir_rail_families(ir: DesignIR) -> dict[str, tuple[float | None, float | None]]:
    """IR 权威轨表:家族 → 电压区间(区间缺失时家族名兜底,再兜不出=None)。"""
    out: dict[str, tuple[float | None, float | None]] = {}
    for rail in ir.power.rails:
        fam = _rail_family(rail.name or rail.v_text())
        lo, hi = _rail_span(rail)
        if lo is None and hi is None:
            v = _family_volts(fam)
            lo = hi = v
        out[fam] = (lo, hi)
    out.pop(_GND_FAMILY, None)
    return out


def _block_category(b, catalog: dict | None) -> str:
    rec = (catalog or {}).get(b.block_id)
    return (rec.category if rec else "") or ""


def _pin_names(b, catalog: dict | None) -> dict[str, str]:
    """脚号→脚名:自由拓扑优先 params._pinout,其次 catalog.pinout。"""
    pinout = (b.params or {}).get("_pinout") or {}
    if not pinout and catalog is not None:
        rec = catalog.get(b.block_id)
        pinout = (rec.pinout if rec else None) or {}
    return {str(k): str(v) for k, v in pinout.items()}


def _is_supply_input(port_name: str, category: str) -> bool:
    """供电输入口判别(端口名 hints,双证据之一)。

    电源类块只认 VIN/VCC/VBUS 输入名(轨名端口在电源类块上是输出侧);
    其余类别按 _PWR_PIN_HINTS 全认(VBAT 在 rtc-ds3231 这类负载上是真输入)。
    含输出 token(VOUT/OUT/BAT/SW)的一律不算输入。
    """
    n = str(port_name).upper()
    if any(t in n for t in _PWR_OUT_TOKENS):
        return False
    if category.lower() == "power":
        return any(h in n for h in _PWR_IN_POWER_CAT)
    return any(h in n for h in _PWR_PIN_HINTS)


def _supply_bindings(b, catalog: dict | None) -> list[tuple[str, str]]:
    """块的 (口名, 绑定网) 供电输入对;ports_binding 直接看口名,pins_binding 经 pinout 脚名。"""
    cat = _block_category(b, catalog)
    out: list[tuple[str, str]] = []
    for port, net in (b.ports_binding or {}).items():
        if _is_supply_input(port, cat):
            out.append((port, net))
    names = _pin_names(b, catalog)
    for pin_no, net in (b.pins_binding or {}).items():
        pin_name = names.get(str(pin_no), str(pin_no))
        if _is_supply_input(pin_name, cat):
            out.append((str(pin_no), net))
    return out


def check_voltage_compat(ir: DesignIR, plan: BlockPlan, catalog: dict | None = None) -> list[Finding]:
    """P4-3① 电压兼容(强门禁):权威 net→电压映射 × 块 v_supply 区间,宽压拐角语义。

    判定只用可证明的越界(block.v_min>rail.v_min 或 block.v_max<rail.v_max,严格不等);
    单边数据/块无数据/网名猜不出 → ELECTRICAL_DATA_DEBT 弱告警,绝不静默也绝不强判。
    """
    findings: list[Finding] = []
    rail_families = _ir_rail_families(ir)
    debt_blocks: list[str] = []
    for b in plan.blocks:
        el = ((catalog or {}).get(b.block_id).electrical) if catalog and catalog.get(b.block_id) else None
        bmin = el.v_supply_min if el else None
        bmax = el.v_supply_max if el else None
        for port, net in _supply_bindings(b, catalog):
            fam = _rail_family(net)
            if fam == _GND_FAMILY:
                continue
            if fam in rail_families:
                lo, hi = rail_families[fam]
            else:
                v = _family_volts(fam)  # 轨家族归一兜底(网名自带电压才可用)
                if v is None:
                    continue  # 猜不出的网名=UNKNOWN,不进强判定
                lo, hi = v, v
            if lo is None and hi is None:
                continue
            if bmin is None and bmax is None:
                if b.instance not in debt_blocks:
                    debt_blocks.append(b.instance)
                continue
            viol_lo = bmin is not None and lo is not None and bmin > lo  # 块下限高于轨下限角(欠压角)
            viol_hi = bmax is not None and hi is not None and bmax < hi  # 块上限低于轨上限角(过压角)
            if viol_lo or viol_hi:
                findings.append(
                    Finding(
                        code="VOLTAGE_OUT_OF_RANGE",
                        where=Where(ref=b.instance, net=net),
                        evidence=(
                            f"{b.instance} 供电口 {port} 绑定网 {net}(电压 {_span_text(lo, hi)})"
                            f",块承受区间 {_span_text(bmin, bmax)} 不覆盖轨角"
                            f"(宽压语义:block.v_min≤rail.v_min 且 block.v_max≥rail.v_max 不满足)"
                        ),
                        severity="error",
                        suggested_fix_class="REPLAN",
                    )
                )
            elif bmin is None or bmax is None:
                # 单边数据:可判的一边没越界,另一边无从证明 → 数据债(弱)
                if b.instance not in debt_blocks:
                    debt_blocks.append(b.instance)
    if debt_blocks:
        findings.append(
            Finding(
                code="ELECTRICAL_DATA_DEBT",
                evidence=(
                    f"供电核对数据债:{len(debt_blocks)} 个块缺 v_supply 区间或单边"
                    f"({', '.join(debt_blocks[:12])}{'…' if len(debt_blocks) > 12 else ''})——"
                    f"无法强判兼容性,按 UNKNOWN 降级;请回填块库 electrical 字段"
                ),
                severity="warn",
                suggested_fix_class="DATA_DEBT",
                weak=True,
            )
        )
    return findings


def _span_text(lo: float | None, hi: float | None) -> str:
    if lo is None and hi is None:
        return "?"
    if lo is not None and hi is not None and lo != hi:
        return f"{lo:g}-{hi:g}V"
    v = lo if lo is not None else hi
    return f"{v:g}V"


def check_current_budget(ir: DesignIR, plan: BlockPlan, catalog: dict | None = None) -> list[Finding]:
    """P4-3② 电流预算(三态):块→轨供关系双证据推导,Σi_typ×1.2 vs 轨 imax。

    负载归属=供电输入口绑定的网家族命中 IR 轨(端口名 hints × 网家族);电压相容性由
    check_voltage_compat 单独负责。三态:OVER(error 强)/ UNKNOWN(warn:容量未知或
    负载覆盖率<100%,evidence 带覆盖率与缺数块清单)/ OK(不出 finding)。
    """
    findings: list[Finding] = []
    rail_families = _ir_rail_families(ir)
    if not rail_families:
        return findings
    rail_by_fam = {
        _rail_family(r.name or r.v_text()): r for r in ir.power.rails if _rail_family(r.name or r.v_text()) != _GND_FAMILY
    }
    loads: dict[str, list[tuple[str, float | None]]] = {}
    for b in plan.blocks:
        seen_fams: set[str] = set()
        for _port, net in _supply_bindings(b, catalog):
            fam = _rail_family(net)
            if fam in rail_families and fam not in seen_fams:
                seen_fams.add(fam)  # 同块多口绑同一轨只计一次
                el = ((catalog or {}).get(b.block_id).electrical) if catalog and catalog.get(b.block_id) else None
                loads.setdefault(fam, []).append((b.instance, el.i_typ if el else None))
    for fam, items in loads.items():
        if not items:
            continue
        rail = rail_by_fam.get(fam)
        name = (rail.name if rail else None) or fam
        known = [(inst, it) for inst, it in items if it is not None]
        missing = [inst for inst, it in items if it is None]
        coverage = len(known) / len(items)
        imax = rail.imax if rail else None
        if imax is None:
            findings.append(
                Finding(
                    code="RAIL_BUDGET_UNKNOWN",
                    where=Where(net=name),
                    evidence=(
                        f"轨 {name} 电流预算 UNKNOWN:轨容量 imax 未知(IR 未声明)"
                        f";已知负载 {len(known)}/{len(items)}(覆盖率 {coverage:.0%})"
                        f"{'' if not missing else ',缺 i_typ:' + ', '.join(missing[:8])}"
                    ),
                    severity="warn",
                    suggested_fix_class="DATA_DEBT",
                    weak=True,
                )
            )
            continue
        if missing:
            findings.append(
                Finding(
                    code="RAIL_BUDGET_UNKNOWN",
                    where=Where(net=name),
                    evidence=(
                        f"轨 {name} 电流预算 UNKNOWN:负载覆盖率 {coverage:.0%}"
                        f"({len(known)}/{len(items)}),缺 i_typ 块: {', '.join(missing[:8])}"
                        f"{'…' if len(missing) > 8 else ''}——已知部分 Σ={sum(it for _, it in known):.3f}A×1.2"
                        f"={sum(it for _, it in known) * 1.2:.3f}A vs imax {imax:g}A,无法整轨判定"
                    ),
                    severity="warn",
                    suggested_fix_class="DATA_DEBT",
                    weak=True,
                )
            )
            continue
        total = sum(it for _, it in known) * 1.2  # 1.2 裕量系数(§9.4)
        if total > imax:
            findings.append(
                Finding(
                    code="RAIL_BUDGET_OVER",
                    where=Where(net=name),
                    evidence=(
                        f"轨 {name} 电流预算超载:Σi_typ={sum(it for _, it in known):.3f}A"
                        f"×1.2 裕量={total:.3f}A > imax {imax:g}A"
                        f";负载: {', '.join(f'{inst}({it:g}A)' for inst, it in known)}"
                    ),
                    severity="error",
                    suggested_fix_class="REPLAN",
                )
            )
    return findings


def check_rails(ir: DesignIR, plan: BlockPlan) -> list[Finding]:
    findings: list[Finding] = []
    bound_families = {_rail_family(net) for b in plan.blocks for net in b.ports_binding.values()}
    bound_families |= {_rail_family(net) for b in plan.blocks for net in b.pins_binding.values()}
    for rail in ir.power.rails:
        name = rail.name or rail.v_text()
        want_family = _rail_family(name)
        if want_family.endswith("|main") and _rail_family(name).split("|")[0] == "GND":
            continue
        if want_family not in bound_families:
            findings.append(
                Finding(
                    code="MISSING_RAIL",
                    where=Where(net=name),
                    evidence=f"DesignIR 电源轨 {name}({rail.v_text()}) 在 BlockPlan 任何端口绑定中都未出现(按轨家族 {_rail_family(name)} 归一比对)",
                    severity="error",
                    suggested_fix_class="REBIND_NET",
                )
            )
    # 反向(P4-3③):plan 里呈轨特征的网(家族可解析出电压)但 IR 未声明 → 弱告警,
    # 提示 planner 显式声明轨或改绑既有轨;猜不出电压的网名不告(无判据不猜)。
    declared = {_rail_family(r.name or r.v_text()) for r in ir.power.rails} | {_rail_family("GND")}
    flagged: set[str] = set()
    for b in plan.blocks:
        for net in [*b.ports_binding.values(), *b.pins_binding.values()]:
            fam = _rail_family(net)
            if fam in flagged or fam in declared or _family_volts(fam) is None:
                continue
            flagged.add(fam)
            findings.append(
                Finding(
                    code="UNDECLARED_RAIL",
                    where=Where(net=net),
                    evidence=f"网 {net} 呈轨特征(家族 {fam}≈{_family_volts(fam)}V)但 DesignIR 未声明该轨;planner 应在 IR 电源段显式声明,或把该网改绑到既有轨家族",
                    severity="warn",
                    suggested_fix_class="REPLAN",
                    weak=True,
                )
            )
    return findings


def check_uncovered(plan: BlockPlan) -> list[Finding]:
    return [
        Finding(
            code="IR_UNCOVERED",
            evidence=item,
            severity="warn",
            suggested_fix_class="ADD_BLOCK",
            weak=True,
        )
        for item in plan.uncovered
    ]


# ---------- P4-4③ 参数核对:实际选值 vs sizing 建议值(弱观察) ----------

# 偏差容忍 = ±1 个 E24 档(E24 相邻档比 ~1.1,两档 ~1.21;1.35 覆盖档间不均匀)
_PARAM_TOLERANCE = 1.35


def _value_num(kind: str, text: str) -> float | None:
    """'4.7k'→4700Ω / '100n'→1e-9F;解析不出返回 None(canon 后按尾缀换算)。"""
    from edaloop.generate.stdparts import canon_value

    key = canon_value(kind, text)
    m = re.fullmatch(r"([\d.]+)([Mknup]?)", key)
    if not m:
        return None
    v = float(m.group(1))
    scale = {"resistor": {"": 1.0, "k": 1e3, "M": 1e6}, "capacitor": {"": 1e-6, "u": 1e-6, "n": 1e-9, "p": 1e-12}}[kind]
    return v * scale.get(m.group(2), 1.0)


def check_param_off_spec(plan: BlockPlan, sizing_advices, catalog: dict | None = None) -> list[Finding]:
    """P4-4③ 实际选值 vs sizing 建议 E24 归一比对(先弱观察;连续 2 批零误报再议转强)。

    比对通道(确定性,无 LLM):
      A. 直接:advice.target == plan 块 instance 且该块声明 params.value(同 quantity);
      B. 网组:std-value R/C 块按 advice.nets 聚组——电容取 pins 网集 == advice.nets 的
         set 相等(输出电容 {VOUT,GND} 无歧义);LED 限流电阻取「与建议驱动网恰有一网相交」
         (串联件必触驱动节点,另一端是内部节点)。**组内任一成员在容差内即满足**
         (22µF 纹波电容与 100n 去耦电容共存于同网对,是正常设计,不逐件判罪)。
    偏差超 1 个 E24 档(|actual/rec| > 1.35 或 < 1/1.35)且组内无任何合格成员 →
    PARAM_OFF_SPEC(weak, REPLAN)。缺件(建议有、图上无)**不在此报**——块内可能已含
    该元件(led-indicator 内置限流电阻),缺件归 critic/refine 问答(采纳补件),避免误杀。
    """
    from edaloop.generate.stdparts import kind_of

    qty_of_kind = {"resistor": "resistance", "capacitor": "capacitance"}
    recs = [a for a in (sizing_advices or []) if getattr(a, "rec_value", "") and getattr(a, "rec_kind", "") in qty_of_kind.values()]
    if not recs:
        return []
    kind_of_qty = {v: k for k, v in qty_of_kind.items()}
    by_target: dict[str, object] = {}
    for a in recs:
        by_target.setdefault(a.target, a)
    findings: list[Finding] = []

    def _num(qty: str, text: str) -> float | None:
        return _value_num(kind_of_qty[qty], text)

    def _in_tol(qty: str, val: str, rec_val: str) -> bool:
        actual, want = _num(qty, val), _num(qty, rec_val)
        if not actual or not want:
            return True  # 解析不出的值不判罪(容差内处理)
        ratio = actual / want
        if qty == "capacitance":
            # 电容只判欠额:超规格是工程裕量(纹波更小),不是缺陷
            return ratio >= 1 / _PARAM_TOLERANCE
        return _PARAM_TOLERANCE >= ratio >= 1 / _PARAM_TOLERANCE

    def _flag(advice, members: list) -> Finding:
        srcs = "; ".join(f"{n}={v}←{s}" for n, v, s in (getattr(advice, "inputs", None) or [])[:3])
        vals = ", ".join(f"{m.instance}={m.params.get('value', '?')}" for m in members[:4])
        return Finding(
            code="PARAM_OFF_SPEC",
            where=Where(ref=members[0].instance),
            evidence=(
                f"选值 [{vals}] vs sizing 建议 {advice.rec_value}({advice.kind}@{advice.target});"
                f"该网组内无任何 E24 邻档合格成员;建议依据: {srcs}"
            ),
            severity="warn",
            suggested_fix_class="REPLAN",
            weak=True,
        )

    # ---- 通道 A:直接(块自己声明了值,且是建议目标) ----
    consumed: set[int] = set()
    for b in plan.blocks:
        val = (b.params or {}).get("value", "")
        if not val:
            continue
        advice = by_target.get(b.instance)
        if advice is None:
            continue
        rec = (catalog or {}).get(b.block_id)
        std_kind = kind_of(rec) if rec is not None else None
        qty = qty_of_kind.get(std_kind or "")
        if qty and qty != advice.rec_kind:
            continue
        if not _in_tol(advice.rec_kind, val, advice.rec_value):
            findings.append(_flag(advice, [b]))
        consumed.add(id(advice))

    # ---- 通道 B:网组(std-value 元件按 advice.nets 聚组,组内任一合格即满足) ----
    std_blocks = []
    for b in plan.blocks:
        if not (b.params or {}).get("value"):
            continue
        rec = (catalog or {}).get(b.block_id)
        std_kind = kind_of(rec) if rec is not None else None
        if std_kind:
            std_blocks.append((b, qty_of_kind[std_kind], set((b.pins_binding or {}).values())))
    for advice in recs:
        if id(advice) in consumed or not getattr(advice, "nets", None):
            continue
        anets = set(advice.nets)
        if advice.rec_kind == "capacitance" and len(anets) == 2:
            group = [(b, q, ns) for b, q, ns in std_blocks if q == advice.rec_kind and ns == anets]
        elif advice.rec_kind == "resistance" and advice.kind == "led-resistor" and len(anets) == 1:
            drive = next(iter(anets))
            if _family_volts(_rail_family(drive)) is not None:
                # 驱动网本身是轨(电源指示灯直挂轨):该节点上的外部 R 可能是上拉/分压,
                # 与限流串联电阻拓扑不可分,不判(缺件归 critic/refine 问答)
                continue
            group = [(b, q, ns) for b, q, ns in std_blocks if q == advice.rec_kind and len(ns & anets) == 1]
        else:
            continue
        if not group:
            continue  # 缺件不报(块内可能已含;归 critic/refine 问答)
        if not any(_in_tol(advice.rec_kind, b.params.get("value", ""), advice.rec_value) for b, _q, _ns in group):
            findings.append(_flag(advice, [b for b, _q, _ns in group]))
    return findings


def check_gauge(gate_report: dict) -> list[Finding]:
    findings: list[Finding] = []
    verdict = gate_report.get("verdict", "unknown")
    if verdict == "pass":
        return findings
    if verdict == "blocked":
        findings.append(
            Finding(
                code="GATE_BLOCKED",
                evidence="部分检查器无法运行(连接器/页面问题),板子未被完整判定",
                severity="error",
                suggested_fix_class="RETRY_ENV",
            )
        )
    for stage in gate_report.get("stages", []):
        name = stage.get("stage") or stage.get("name") or "?"
        sv = stage.get("verdict") or stage.get("status") or ""
        err = stage.get("error") or ""
        if sv == "pass" or sv == "skipped":
            continue
        detail = stage.get("detail") if isinstance(stage.get("detail"), dict) else {}
        items = (
            stage.get("findings")
            or stage.get("blockers")
            or detail.get("findings")
            or detail.get("blockers")
            or detail.get("overlaps")
            or detail.get("items")
            or []
        )
        if err:
            findings.append(
                Finding(
                    code="GATE_FAIL",
                    where=Where(ref=name),
                    evidence=f"stage {name}: {err}",
                    severity="error",
                    suggested_fix_class="RETRY_ENV",
                )
            )
        for f in items[:20]:
            findings.append(
                Finding(
                    code="GATE_FAIL",
                    where=Where(ref=name),
                    evidence=_compact(f),
                    severity="error",
                    suggested_fix_class=_fix_class(name, f),
                )
            )
    if verdict == "fail" and not findings:
        findings.append(
            Finding(
                code="GATE_FAIL",
                evidence=f"sch gate verdict=fail 但未给出分项明细(共 {len(gate_report.get('stages', []))} 阶段)",
                severity="error",
                suggested_fix_class="REPLAN",
            )
        )
    return findings


def _compact(f: object) -> str:
    if isinstance(f, dict):
        parts = []
        for k in ("code", "type", "message", "detail", "net", "ref", "designator"):
            if f.get(k):
                parts.append(f"{k}={f[k]}")
        return " ".join(parts)[:200] or str(f)[:200]
    return str(f)[:200]


def _fix_class(stage: str, f: object) -> str:
    text = str(f).lower()
    if "overlap" in text or "layout" in stage:
        return "RELAYOUT"
    if "dangl" in text or "floating" in text:
        return "REWIRE"
    if "short" in text or "bridge" in text:
        return "REWIRE"
    return "REPLAN"


_PWR_PIN_HINTS = ("VCC", "VDD", "VIN", "VBAT", "VBUS", "VDDIO", "VDDA", "VSYS", "5V", "3V3")
_GND_PIN_HINTS = ("GND", "VSS", "AGND", "DGND", "PGND")


def check_topology_sanity(
    plan: BlockPlan, catalog: dict | None = None
) -> list[Finding]:
    """自由拓扑 sanity(强门禁):每个 place 通道器件的电源/地引脚必须已绑定到网。"""
    findings: list[Finding] = []
    for b in plan.blocks:
        if b.upstream_id:
            continue
        pinout = (b.params or {}).get("_pinout") or {}
        if not pinout:
            pinout = _catalog_pinout(b.block_id) if catalog is None else (catalog.get(b.block_id).pinout if catalog.get(b.block_id) else {})
        if not pinout:
            continue
        bound = set(b.pins_binding)
        for pin_no, pin_name in pinout.items():
            n = str(pin_name).upper()
            if any(h in n for h in _PWR_PIN_HINTS) and pin_no not in bound:
                findings.append(
                    Finding(
                        code="PIN_MISMATCH",
                        where=Where(ref=b.instance, pin=pin_no),
                        evidence=f"自由拓扑器件 {b.instance} 的电源脚 {pin_no}({pin_name}) 未绑定网络",
                        severity="error",
                        suggested_fix_class="REBIND_NET",
                    )
                )
            if any(h in n for h in _GND_PIN_HINTS) and pin_no not in bound:
                findings.append(
                    Finding(
                        code="PIN_MISMATCH",
                        where=Where(ref=b.instance, pin=pin_no),
                        evidence=f"自由拓扑器件 {b.instance} 的地脚 {pin_no}({pin_name}) 未绑定网络",
                        severity="error",
                        suggested_fix_class="REBIND_NET",
                    )
                )
    return findings


def _catalog_pinout(block_id: str) -> dict:
    return {}


def validate(
    ir: DesignIR,
    plan: BlockPlan,
    gate_report: dict | None,
    *,
    catalog: dict | None = None,
    sizing: list | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    findings += check_rails(ir, plan)
    findings += check_voltage_compat(ir, plan, catalog)
    findings += check_current_budget(ir, plan, catalog)
    findings += check_uncovered(plan)
    findings += check_topology_sanity(plan, catalog)
    if sizing:
        # P4-4③:sizing 建议值与实际选值的弱观察比对(controller 轮内计算后传入)
        findings += check_param_off_spec(plan, sizing, catalog)
    if gate_report is not None:
        findings += check_gauge(gate_report)
    return findings
