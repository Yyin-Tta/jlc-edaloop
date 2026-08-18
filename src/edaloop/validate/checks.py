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


def check_rails(ir: DesignIR, plan: BlockPlan) -> list[Finding]:
    findings: list[Finding] = []
    bound = {norm_rail(net) for b in plan.blocks for net in b.ports_binding.values()}
    for rail in ir.power.rails:
        name = rail.name or f"{rail.voltage:g}V"
        want = norm_rail(name)
        if want == "GND":
            continue
        if want not in bound:
            findings.append(
                Finding(
                    code="MISSING_RAIL",
                    where=Where(net=want),
                    evidence=f"DesignIR 电源轨 {name}({rail.voltage:g}V) 在 BlockPlan 任何端口绑定中都未出现",
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


def validate(
    ir: DesignIR, plan: BlockPlan, gate_report: dict | None
) -> list[Finding]:
    findings: list[Finding] = []
    findings += check_rails(ir, plan)
    findings += check_uncovered(plan)
    if gate_report is not None:
        findings += check_gauge(gate_report)
    return findings
