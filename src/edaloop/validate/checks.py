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


# ---------- P4-5② 功能覆盖机械对齐:IR.functions × 计划块(恒弱) ----------

# 词表:功能用词(左,在 function 文本里找)→ 块词汇(右,在块联合文本里找),命中即覆盖。
# 拆分原则:左侧专有词(rs-485/usb/充电)必须映射到右侧同类专有词——泛化右侧(usb、esp32)
# 会让「RS-485 通信」被任意 USB-C 座覆盖(P4-5⑤ 注入实证),故接口行按协议拆开。
_FUNC_SYNONYMS: list[tuple[str, str]] = [
    (r"点灯|指示灯|led|灯", r"led|indicator"),
    (r"主控|单片机|mcu|控制器", r"mcu|stm32|单片机|controller"),
    (r"串口|烧录|下载|uart|ttl|日志", r"uart|ch340|serial|usb-serial|cp210"),
    (r"供电|电源|稳压|降压|升压|buck|ldo|电压转换", r"power|ldo|buck|boost|dc-dc|charger"),
    (r"电池|锂电|充电|battery", r"battery|charg|18650|dw01|fs8205|bq25|tp405"),
    (r"按键|按钮|复位|boot|键", r"button|reset|boot|human-input"),
    (r"传感|测量|温湿度|imu|加速度|采集", r"sensing|sensor|imu|adc"),
    (r"显示|屏幕|oled|lcd|屏", r"display|oled|lcd"),
    (r"存储|eeprom|flash|记忆", r"storage|eeprom|flash|sd"),
    (r"通信|无线|wifi|蓝牙|ble|联网|射频|无线", r"comms|rf|wifi|ble|esp32|antenna|cc1101"),
    (r"隔离", r"iso|isolat|b0505|opto"),
    (r"保护|防反|过流|保险|tvs|浪涌", r"tvs|polyfuse|protect|fuse|mosfet"),  # 防反=PMOS 通道
    (r"电机|驱动|马达|继电器|mos", r"driver|motor|mosfet|relay|speaker-amp"),
    (r"时钟|晶振|rtc|计时", r"timing|crystal|rtc|32k"),
    (r"rs-?485|modbus", r"rs485|max485|sp3485|485"),
    (r"usb|type-?c|网口|rj45", r"usb|typec|rj45"),
    (r"接口|端子|排针|插针", r"terminal|interface|header|conn|排针|端子"),
    (r"遥测|上报|数据上传", r"comms|uart|rs485|ble|esp32"),
    (r"低压|告警|欠压", r"lowvolt|alarm|tl431|欠压|低压"),
    (r"测试点|test.?point", r"testpoint|测试点|探针"),
    (r"结构|安装孔|固定孔|螺丝", r"mount|hole|安装孔|螺丝|结构"),
]
_FUNC_STOP = {"and", "for", "with", "the", "via", "pcb", "gpio", "gnd", "vcc", "3v3", "5v", "12v", "24v"}


def _block_corpus(plan: BlockPlan, catalog: dict | None) -> tuple[set[str], set[str], str, set[str]]:
    """计划块联合文本 → (ascii 词元集[全文], CJK 标签文本[仅 name/category/tags/block_id]),小写。

    词元集含 -/_ 融合变体(rs-485 → rs485),按「词元等值/前缀」判,不做全文子串——
    子串会让 2~3 字符右侧词(rf/ble/sd)在任意长词里诈胡(P4-5⑤ 实证:「RS-485
    通信」被语料某处子串 rf 覆盖)。CJK bigram 只对**标签字段**——desc 叙述文里的
    共词(如升压块 desc 提「锂电」)不该让「锂电池充电」算被覆盖,承载件必须自报标签。
    """
    raws: list[str] = []
    labels: list[str] = []
    for b in plan.blocks:
        rec = (catalog or {}).get(b.block_id)
        seg = [b.block_id]
        lab = [b.block_id]
        if rec is not None:
            name = getattr(rec, "name", "") or ""
            desc = getattr(rec, "desc", "") or ""
            cat = getattr(rec, "category", "") or ""
            tags = " ".join(getattr(rec, "tags", None) or [])
            seg += [name, desc, cat, tags]
            lab += [name, cat, tags]
            up = getattr(rec, "upstream", None)
            if up is not None and getattr(up, "id", None):
                seg.append(up.id)
        raws.append(" ".join(x for x in seg if x))
        labels.append(" ".join(x for x in lab if x))
    raw = " ".join(raws).lower()
    lab = " ".join(labels).lower()
    toks = set(re.findall(r"[a-z0-9]+", raw))
    # 标签词元保留连字符整词(mcu-support 不产出碎片 mcu——支持电路不是主控本体;
    # 真 mcu 块的 category/tag 自带独立整词)+ 融合变体
    lab_toks = set(re.findall(r"[a-z0-9][a-z0-9_-]*", lab))
    for t in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", raw):
        fused = re.sub(r"[-_]", "", t)
        if len(fused) >= 3:
            toks.add(fused)
    for t in list(lab_toks):
        fused = re.sub(r"[-_]", "", t)
        if len(fused) >= 3:
            lab_toks.add(fused)
    cjk = " ".join(re.findall(r"[一-鿿]+", lab))
    # 伪块(自由拓扑实例,block_id 不在目录)= planner 的承载声明,其 id 词元可作救援证据
    pseudo_toks: set[str] = set()
    for b in plan.blocks:
        if (catalog or {}).get(b.block_id) is None:
            pseudo_toks.add(b.block_id.lower())
            for part in re.split(r"[-_]", b.block_id.lower()):
                if len(part) >= 3:
                    pseudo_toks.add(part)
            fused = re.sub(r"[-_]", "", b.block_id.lower())
            if len(fused) >= 3:
                pseudo_toks.add(fused)
    return toks, lab_toks, cjk, pseudo_toks


# 修饰词:含此词的标签词元是「服务于 X 的电路」而非 X 本体(mcu-support ≠ 主控)
_LAB_QUALIFIERS = ("support", "isolat")


def _right_hits(brx: str, toks: set[str], lab_toks: set[str], cjk: str) -> bool:
    """词表右侧对语料:≥5 字符右词可命中全文词元(前缀);短右词只认**标签**词元——
    叙述文里的共词(ch340 desc 的「目标 MCU」)不算;标签侧允许前缀(mcu_main 是
    planner 声明的主控实例)但含修饰词的复合词不算(mcu-support/mcusupport)。
    CJK 右词做标签子串。"""
    for t in toks:
        m = re.match(brx, t)
        if m and len(m.group(0)) >= 5:
            return True
    for t in lab_toks:
        m = re.match(brx, t)
        if m and not any(q in t for q in _LAB_QUALIFIERS):
            return True
    return bool(cjk and re.search(brx, cjk))


def _func_probe_hits(text: str, toks: set[str], lab_toks: set[str], cjk: str) -> bool:
    """单一探测文本对语料:词表(双语)→ ascii 词元(短词认标签/长词认全文)→ CJK bigram,任一命中。"""
    for frx, brx in _FUNC_SYNONYMS:
        if re.search(frx, text) and _right_hits(brx, toks, lab_toks, cjk):
            return True
    for tok in set(re.findall(r"[a-z0-9][a-z0-9_-]{2,}", text)):
        if tok in _FUNC_STOP:
            continue
        fused = re.sub(r"[-_]", "", tok)
        if len(fused) < 5:
            # 短词元只认整词精确(标签词元保留连字符,mcu 不得前缀命中 mcusupport)
            if fused in lab_toks:
                return True
        elif fused in toks or any(t.startswith(fused) for t in toks):
            return True
    for run in re.findall(r"[一-鿿]{2,}", text):
        for i in range(len(run) - 1):
            if run[i : i + 2] in cjk:
                return True
    return False


def _func_has_signal(text: str) -> bool:
    """文本里是否提取得到任何可判信号(词表左词/ascii 词元/CJK bigram)——没有则该层探测无效。"""
    if any(re.search(frx, text) for frx, _ in _FUNC_SYNONYMS):
        return True
    if any(tok not in _FUNC_STOP for tok in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", text)):
        return True
    return bool(re.findall(r"[一-鿿]{2,}", text))


_FUNC_SPECIFIC_MIN = 5  # ≥5 字符的 ascii 词元视为专名(tl431/ch340/sp3485),可作 desc 侧救援证据


def _func_covered(name: str, full: str, corpus: tuple[set[str], set[str], str, set[str]]) -> bool:
    """两级探测:**name 优先**——功能名是规范标签,desc/constraints 只是展开。

    name 有可判信号时以 name 的命中为准(防 desc 里的泛化词把缺口“覆盖”掉,
    如「锂电池充电」的 desc 提到 5V 输入就被任意 power 块覆盖);desc 只允许
    **专名词元**(≥5 字符,如 tl431/ch340n)救援,且证据只认**伪块实例名**——
    实例名是 planner 的承载声明(alarm_tl431/do_uln),目录块 name 里的型号
    碎片不算(up-esp32_autodownload 的 esp32 救不了「MCU 主控」)。
    name 提取不到信号(空名/纯停用词)才退化用全文全机制。
    """
    toks, lab_toks, cjk, pseudo_toks = corpus
    if name.strip() and _func_probe_hits(name, toks, lab_toks, cjk):
        return True
    if name.strip() and _func_has_signal(name):
        for tok in set(re.findall(r"[a-z0-9][a-z0-9_-]{2,}", full)):
            fused = re.sub(r"[-_]", "", tok)
            if len(fused) < _FUNC_SPECIFIC_MIN:
                continue
            # 直证(实例名即含型号,alarm_tl431)或反向前缀(do_uln ↔ uln2003)
            if fused in pseudo_toks or any(fused.startswith(t) for t in pseudo_toks):
                return True
        return False
    return bool(full.strip()) and _func_probe_hits(full, toks, lab_toks, cjk)


def check_func_covered(ir: DesignIR, plan: BlockPlan, catalog: dict | None = None) -> list[Finding]:
    """P4-5② IR.functions × 块 tags/category 机械对齐;漏覆盖 → FUNC_UNCOVERED。

    **恒弱**:词表/词元/bigram 是启发式映射,不满足强门禁「能机械证明未覆盖」的前提
    (证明「覆盖」靠相似性,证明「未覆盖」不可能排除同义表达)。is_blocking 的
    severity=="error" 兜底会误伤,故 weak=True 且 severity="warn" 双保险。
    """
    corpus = _block_corpus(plan, catalog)
    findings: list[Finding] = []
    for f in ir.functions:
        fname = (f.name or "").lower()
        ftext = " ".join([f.name, f.desc, f.constraints_digest()]).lower()
        if not ftext.strip() or _func_covered(fname, ftext, corpus):
            continue
        findings.append(
            Finding(
                code="FUNC_UNCOVERED",
                where=Where(ref=(f.name or f.desc)[:40]),
                evidence=(
                    f"IR 功能「{f.name or f.desc[:30]}」在计划 {len(plan.blocks)} 块的词表/词元匹配中无覆盖证据"
                    "(启发式对齐,恒弱;确属需要时补块,或 refine 补充需求细节)"
                ),
                severity="warn",
                suggested_fix_class="ADD_BLOCK",
                weak=True,
            )
        )
    return findings


# ---------- P4-5① 验收条目机械复评(ACCEPTANCE_UNMET,恒弱) ----------


def check_acceptance(
    ir: DesignIR,
    plan: BlockPlan,
    items: list,
    gate_report: dict | None = None,
    catalog: dict | None = None,
) -> list[Finding]:
    """AcceptanceItem → 调映射 checker 子集复评;有 hard finding → ACCEPTANCE_UNMET(弱)。

    只把 severity=error 且非 weak 的 finding 记为未满足(数据债/UNKNOWN 是降级不是失败);
    manual 条目不判。rail/budget 条目按轨家族过滤,别的轨的既有 finding 不牵连本条。
    """
    from edaloop.intent.acceptance import is_executable

    findings: list[Finding] = []
    for it in items:
        if not is_executable(it.checker):
            continue
        names = set(it.checker.split("+"))
        sub: list[Finding] = []
        if "check_rails" in names:
            sub += check_rails(ir, plan)
        if "check_voltage_compat" in names:
            sub += check_voltage_compat(ir, plan, catalog)
        if "check_current_budget" in names:
            sub += [f for f in check_current_budget(ir, plan, catalog) if f.code == "RAIL_BUDGET_OVER"]
        if "check_func_covered" in names:
            sub += check_func_covered(ir, plan, catalog)
        if "check_topology_sanity" in names:
            sub += check_topology_sanity(plan, catalog)
        if "check_gauge" in names:
            if gate_report is None:
                continue  # 规划期/dry-run 无 gate 报告:不判,不冒误报
            sub += check_gauge(gate_report)
        if it.kind in ("rail", "budget") and it.key:
            want = _rail_family(it.key)
            sub = [f for f in sub if (f.where.net and _rail_family(f.where.net) == want) or not f.where.net]
        hard = [f for f in sub if f.severity == "error" and not f.weak]
        if hard:
            findings.append(
                Finding(
                    code="ACCEPTANCE_UNMET",
                    where=Where(ref=it.id, net=it.key),
                    evidence=(
                        f"[{it.id}] {it.check}: 期望「{it.expect[:60]}」未满足 ← "
                        + "; ".join(f"{f.code}: {f.evidence[:80]}" for f in hard[:2])
                    ),
                    severity="warn",
                    suggested_fix_class="REPLAN",
                    weak=True,
                )
            )
    return findings


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
    acceptance: list | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    findings += check_rails(ir, plan)
    findings += check_voltage_compat(ir, plan, catalog)
    findings += check_current_budget(ir, plan, catalog)
    findings += check_uncovered(plan)
    findings += check_topology_sanity(plan, catalog)
    # P4-5②:功能覆盖机械对齐(恒弱——词表/词元启发式不满足强门禁前提)
    findings += check_func_covered(ir, plan, catalog)
    if sizing:
        # P4-4③:sizing 建议值与实际选值的弱观察比对(controller 轮内计算后传入)
        findings += check_param_off_spec(plan, sizing, catalog)
    if acceptance:
        # P4-5①:验收条目机械复评(恒弱;manual 条目不判)
        findings += check_acceptance(ir, plan, acceptance, gate_report, catalog)
    if gate_report is not None:
        findings += check_gauge(gate_report)
    return findings
