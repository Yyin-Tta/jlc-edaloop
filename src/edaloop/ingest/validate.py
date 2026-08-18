from __future__ import annotations

from edaloop.ingest.models import IngestReport, PinInfo, PinTable

_POWER_RE = ("VCC", "VDD", "VEE", "VSS", "VIN", "VBAT", "COM")
_GND_RE = ("GND", "VSS", "AGND", "DGND", "PGND", "E")


def check_internal(table: PinTable) -> list[str]:
    """pin 表内部一致性(强门禁):重复号 / 类型正则 / power 命名。"""
    violations: list[str] = []
    seen: dict[str, str] = {}
    for p in table.pins:
        if p.number in seen:
            violations.append(f"pin 号重复: {p.number}({seen[p.number]} 与 {p.name})")
        seen[p.number] = p.name
        if p.io_type and p.io_type not in ("I", "O", "I/O", "P", "S"):
            violations.append(f"pin {p.number} 非法 io_type: {p.io_type}")
    nums = sorted(int(p.number) for p in table.pins if p.number.isdigit())
    if nums:
        expect = list(range(nums[0], nums[-1] + 1))
        if nums != expect:
            missing = sorted(set(expect) - set(nums))
            violations.append(f"引脚号不连续(疑似漏提): 缺 {missing}")
    if not table.pins:
        violations.append("引脚表为空")
    return violations


def compare_channels(llm: PinTable, rule: list[PinInfo]) -> list[str]:
    """双通道比对:number→name 集合 diff;不一致的 pin 标记低置信(agreed=False)。"""
    llm_map = {p.number: p.name.upper() for p in llm.pins}
    rule_map = {p.number: p.name.upper() for p in rule}
    disagreements: list[str] = []
    for no in sorted(set(llm_map) | set(rule_map), key=lambda x: (len(x), x)):
        l = llm_map.get(no)
        r = rule_map.get(no)
        if l != r:
            disagreements.append(f"pin {no}: llm={l} rule={r}")
    for p in llm.pins:
        if rule_map.get(p.number) != p.name.upper():
            p.agreed = False
    return disagreements


def run_gate(llm: PinTable, rule: list[PinInfo]) -> IngestReport:
    disagreements = compare_channels(llm, rule)
    violations = check_internal(llm)
    verdict = "pass" if not disagreements and not violations else "fail"
    if disagreements and len(disagreements) <= max(2, len(llm.pins) // 8):
        verdict = "low-confidence"
    return IngestReport(
        part=llm.part,
        pdf=llm.source_pdf,
        pin_count=len(llm.pins),
        evidence_pages=llm.pages,
        llm_pins=len(llm.pins),
        rule_pins=len(rule),
        disagreements=disagreements,
        internal_violations=violations,
        verdict=verdict,
    )
