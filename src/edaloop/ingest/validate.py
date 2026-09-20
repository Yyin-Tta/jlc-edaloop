from __future__ import annotations

from edaloop.ingest.models import IngestReport, PinInfo, PinTable

_POWER_RE = ("VCC", "VDD", "VEE", "VSS", "VIN", "VBAT", "COM")
_GND_RE = ("GND", "VSS", "AGND", "DGND", "PGND", "E")


def _missing_nums(table: PinTable) -> list[int]:
    """引脚号区间内缺的号(内部跳号);空表或全连续 → []。"""
    nums = sorted(int(p.number) for p in table.pins if p.number.isdigit())
    if not nums:
        return []
    return sorted(set(range(nums[0], nums[-1] + 1)) - set(nums))


def check_internal(table: PinTable) -> list[str]:
    """pin 表内部一致性:重复号 / 类型正则 / 跳号 / 空表。跳号(疑似漏提)单列,供 run_gate 分级。"""
    violations: list[str] = []
    seen: dict[str, str] = {}
    for p in table.pins:
        if p.number in seen:
            violations.append(f"pin 号重复: {p.number}({seen[p.number]} 与 {p.name})")
        seen[p.number] = p.name
        if p.io_type and p.io_type not in ("I", "O", "I/O", "P", "S"):
            violations.append(f"pin {p.number} 非法 io_type: {p.io_type}")
    miss = _missing_nums(table)
    if miss:
        violations.append(f"引脚号不连续(疑似漏提): 缺 {miss}")
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
    # 硬违规(一票 fail)= 非跳号违规(重复/非法 io_type/空表)+ 大缺口(≥3 脚漏提);
    # 小缺口(1-2 脚 = 保留/NC 脚)软处理 → low-confidence,不再误杀带保留脚的大 SoC(esp32-s3 缺 46)。
    hard = [v for v in violations if "不连续" not in v]
    miss = _missing_nums(llm)
    if len(miss) >= 3:
        hard.append(f"引脚号不连续(疑似漏提): 缺 {miss}")
    if not rule:
        if hard:
            verdict = "fail"
        else:
            verdict = "low-confidence"
            disagreements = []
    elif not disagreements and not hard:
        verdict = "pass"
    elif disagreements and len(disagreements) <= max(2, len(llm.pins) // 8) and not hard:
        verdict = "low-confidence"
    else:
        verdict = "fail"
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
