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
    """轨名归一到家族(5V_ISO/VISO/+VO → '5|iso');隔离侧惯用名按 5V。"""
    n = name.strip().upper()
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


def check_rails(ir: DesignIR, plan: BlockPlan) -> list[Finding]:
    findings: list[Finding] = []
    bound_families = {_rail_family(net) for b in plan.blocks for net in b.ports_binding.values()}
    bound_families |= {_rail_family(net) for b in plan.blocks for net in b.pins_binding.values()}
    for rail in ir.power.rails:
        name = rail.name or f"{rail.voltage:g}V"
        want_family = _rail_family(name)
        if want_family.endswith("|main") and _rail_family(name).split("|")[0] == "GND":
            continue
        if want_family not in bound_families:
            findings.append(
                Finding(
                    code="MISSING_RAIL",
                    where=Where(net=name),
                    evidence=f"DesignIR 电源轨 {name}({rail.voltage:g}V) 在 BlockPlan 任何端口绑定中都未出现(按轨家族 {_rail_family(name)} 归一比对)",
                    severity="error",
                    suggested_fix_class="REBIND_NET",
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
    ir: DesignIR, plan: BlockPlan, gate_report: dict | None, *, catalog: dict | None = None
) -> list[Finding]:
    findings: list[Finding] = []
    findings += check_rails(ir, plan)
    findings += check_uncovered(plan)
    findings += check_topology_sanity(plan, catalog)
    if gate_report is not None:
        findings += check_gauge(gate_report)
    return findings
