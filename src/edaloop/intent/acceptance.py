"""P4-5① 需求→可验证规格:AcceptanceSpec 生成器 + 验收↔checker 映射表。

条目两路合流(同一 AcceptanceItem):
  A. IR 派生(所有需求恒有):电源轨表→check_rails+check_voltage_compat、
     带载轨→check_current_budget、接口表→check_func_covered、保护→check_func_covered;
  B. 「## 期望指标」标注段(不再丢弃):markdown 表格行→按关键词规则映射 checker。

映射表 = _MD_RULES(标注行)+ _IR_CHECKERS(IR 派生,构造时直接给 checker)。
可执行性 = checker 名全部 ∈ IMPLEMENTED_CHECKERS;manual 条目只入清单不判
(无 checker 不强判,§3 确定性边界)。判罪由 validate 段 check_acceptance 机械复评,
FAIL → ACCEPTANCE_UNMET(恒弱,归 refine 问题队列)。
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from edaloop.intent.ir import DesignIR


class AcceptanceItem(BaseModel):
    id: str  # R#(IR 轨)/B#(IR 预算)/I#(IR 接口)/P#(IR 保护)/M#(标注表行)
    source: str  # "ir" | "md"
    kind: str  # rail | budget | interface | protection | blocks | nets | gate | manual
    check: str  # 检查项(人可读)
    expect: str  # 期望(人可读)
    checker: str  # "+" 连接的已实现 checker 名,或 "manual"
    key: str = ""  # 机械过滤键(rail 家族名/行号)


# 已实现 checker 清单(可执行率的判定口径;新 checker 落地后在此登记)
IMPLEMENTED_CHECKERS = {
    "check_rails",
    "check_voltage_compat",
    "check_current_budget",
    "check_func_covered",
    "check_topology_sanity",
    "check_gauge",
}

# 标注表行 → (kind, checker):按序首个关键词命中定判;都不中 = manual
_MD_RULES: list[tuple[tuple[str, ...], str, str]] = [
    (("电源树", "电源轨", "供电"), "rail", "check_rails+check_voltage_compat"),
    (("预算", "电流"), "budget", "check_current_budget"),
    (("必含块", "必含", "块清单"), "blocks", "check_func_covered"),
    (("网络", "netlist", "网表"), "nets", "check_topology_sanity"),
    (("门禁", "gate", "drc"), "gate", "check_gauge"),
    (("丝印", "标注"), "manual", "manual"),
    (("结构", "孔", "天线"), "manual", "manual"),
    (("器件数", "bom"), "manual", "manual"),
]


def is_executable(checker: str) -> bool:
    if checker == "manual":
        return False
    return all(p in IMPLEMENTED_CHECKERS for p in checker.split("+"))


def _map_md_row(check: str, expect: str) -> tuple[str, str]:
    text = f"{check} {expect}".lower()
    for keys, kind, checker in _MD_RULES:
        if any(k in text for k in keys):
            return kind, checker
    return "manual", "manual"


def parse_acceptance(md_text: str) -> list[AcceptanceItem]:
    """「## 期望指标」标注段 → 条目列表(确定性表格解析,无 LLM;无该段返回空)。"""
    m = re.search(r"^##\s*期望指标.*$", md_text or "", re.MULTILINE)
    if not m:
        return []
    section = md_text[m.end():]
    items: list[AcceptanceItem] = []
    for line in section.splitlines():
        s = line.strip()
        if not (s.startswith("|") and s.count("|") >= 3):
            continue
        cols = [c.strip() for c in s.strip("|").split("|")]
        if len(cols) < 3 or not re.fullmatch(r"\d+", cols[0]):
            continue  # 表头/分隔行/异形行
        _, check, expect = cols[0], cols[1], " ".join(cols[2:])
        kind, checker = _map_md_row(check, expect)
        items.append(
            AcceptanceItem(
                id=f"M{cols[0]}", source="md", kind=kind, check=check, expect=expect,
                checker=checker, key=cols[0],
            )
        )
    return items


def items_from_ir(ir: DesignIR) -> list[AcceptanceItem]:
    """IR 派生条目:轨/预算/接口/保护(所有需求恒有,不依赖标注段)。"""
    items: list[AcceptanceItem] = []
    for i, r in enumerate(ir.power.rails, 1):
        rail_key = r.name or r.v_text()
        items.append(
            AcceptanceItem(
                id=f"R{i}", source="ir", kind="rail", key=rail_key,
                check=f"电源轨 {r.v_text()}{' ' + rail_key if rail_key not in r.v_text() else ''}".strip(),
                expect="计划端口绑定出现该轨家族,且电压兼容(无超规格器件)",
                checker="check_rails+check_voltage_compat",
            )
        )
        if r.imax is not None:
            items.append(
                AcceptanceItem(
                    id=f"B{i}", source="ir", kind="budget", key=rail_key,
                    check=f"{rail_key} 轨电流预算 ≤{r.imax:g}A",
                    expect="轨上负载电流估计不超 imax(UNKNOWN 数据债不算失败)",
                    checker="check_current_budget",
                )
            )
    for i, itf in enumerate(ir.interfaces, 1):
        items.append(
            AcceptanceItem(
                id=f"I{i}", source="ir", kind="interface", key=itf.type,
                check=f"接口 {itf.type}".strip(),
                expect=f"接口在计划中有对应功能块/网{(': ' + itf.spec[:60]) if itf.spec else ''}".strip(),
                checker="check_func_covered",
            )
        )
    if ir.power.protection:
        items.append(
            AcceptanceItem(
                id="P1", source="ir", kind="protection", key="protection",
                check=f"电源保护 {ir.power.protection[:60]}",
                expect="保护器件(TVS/保险丝/防反)在计划中有对应块",
                checker="check_func_covered",
            )
        )
    return items


def build_acceptance(ir: DesignIR, md_text: str = "") -> list[AcceptanceItem]:
    """全量条目 = IR 派生 + 标注段(eval 通道喂全文时标注行也进来)。"""
    return items_from_ir(ir) + parse_acceptance(md_text)
