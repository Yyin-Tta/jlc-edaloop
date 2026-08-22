"""P3-4 critic 评审 agent(ADR-0009 §P3-4)。

定位:独立 LLM 复核器,审「设计好不好」(机械门禁只证「画对了」)。
维度:去耦完备/上拉下拉/接口保护/热/EMC 设计常识。
输出:结构化 findings(复用 Finding schema,severity=warn)→ 弱门禁,
不阻断交付(原则 2);进 run 目录评审报告 + questions 队列可消费。
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from edaloop.generate.models import BlockPlan
from edaloop.llm.base import ChatMessage, LLMProvider
from edaloop.validate.models import Finding, Where

_SYSTEM = """你是硬件设计评审员(critic)。给定 BlockPlan(块组合+绑定)与设计常识清单,找出**设计层缺陷**(机械校验查不出的)。
只输出 JSON 数组,每项:
{"check": "<检查维度>", "target": "<块实例>", "issue": "问题描述", "advice": "修改建议", "severity": "warn"}

检查维度(逐项过,没问题的不要输出):
1. decoupling:IC/晶体管块缺去耦电容(目录 decoupling-caps-bank 或块内已含则算有)
2. pull-resistors:I2C(SDA/SCL)/复位(EN/NRST)/BOOT 脚缺上拉;开漏输出缺上拉
3. interface-protection:外部接口(USB/RS-485/CAN/端子输入)缺 ESD/TVS;感性负载缺续流
4. power-integrity:电源轨只有单一来源却标注了双输入;LDO 压差/功耗明显超标(>1W 无散热)
5. thermal:功率器件(LDO/BUCK/驱动)无散热提示且电流大
6. emc:晶振/天线附近有高速开关;长线接口无滤波

纪律:
- 只报有把握的问题(证据来自 plan 内容本身,含 netlist_summary/sizing_recommendations);不确定的不报
- 宁缺毋滥:把正确设计标为缺陷比漏报更糟
- sizing_recommendations 已给出的值类建议不要重复报(那是确定性公式;你审的是它覆盖不到的设计层)
- 输出数组可为空 []
"""


def review_plan(
    plan: BlockPlan,
    llm: LLMProvider,
    *,
    catalog_desc: dict[str, str] | None = None,
    netlist_summary: str = "",
    rails_summary: str = "",
    sizing_summary: str = "",
    attempts: int = 2,
) -> list[Finding]:
    catalog_desc = catalog_desc or {}
    blocks_view = []
    for b in plan.blocks:
        # P4-4④ 输入增强:弃 desc-120 截断(块 desc 本就是精炼过的引脚/用途说明,
        # 截断丢掉引脚语义;全量 desc 实测 95 块平均 <200 字符,token 代价可忽略)
        blocks_view.append(
            {
                "instance": b.instance,
                "block_id": b.block_id,
                "upstream": b.upstream_id,
                "ports_binding": b.ports_binding,
                "pins_binding": b.pins_binding,
                "params": {k: v for k, v in (b.params or {}).items() if k in ("value",)},
                "block_desc": catalog_desc.get(b.block_id, ""),
            }
        )
    user_payload: dict = {"blocks": blocks_view, "uncovered": plan.uncovered}
    if netlist_summary:
        user_payload["netlist_summary"] = netlist_summary
    if rails_summary:
        user_payload["ir_rails"] = rails_summary
    if sizing_summary:
        user_payload["sizing_recommendations"] = sizing_summary
    user = json.dumps(user_payload, ensure_ascii=False, indent=1)
    last_raw = ""
    for _ in range(attempts):
        reply = llm.chat([ChatMessage(role="system", content=_SYSTEM), ChatMessage(role="user", content=user)])
        raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", reply.strip()).strip()
        last_raw = raw
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, list):
            continue
        findings = []
        for item in data[:20]:
            if not isinstance(item, dict) or not item.get("issue"):
                continue
            findings.append(
                Finding(
                    code=f"CRITIC_{str(item.get('check', 'general')).upper().replace('-', '_')}",
                    where=Where(ref=str(item.get("target", ""))[:40]),
                    evidence=f"{item.get('issue', '')} | 建议: {item.get('advice', '')}"[:240],
                    severity="warn",
                    suggested_fix_class="ADD_BLOCK" if str(item.get("check")) == "decoupling" else "REPLAN",
                    weak=True,
                )
            )
        return findings
    raise RuntimeError(f"critic 输出解析失败: {last_raw[:200]}")


def render_report(findings: list[Finding], plan_summary: str) -> str:
    lines = [f"# 设计评审报告(critic,弱门禁)", f"评审对象: {plan_summary}", ""]
    if not findings:
        lines.append("(无设计层缺陷发现——注意:仅审设计常识,不替代机械门禁)")
    for f in findings:
        lines.append(f"- [{f.code}@{f.where.ref}] {f.evidence}")
    lines.append("")
    lines.append("(以上为 warn 级建议,不阻断交付;采纳与否人工决定)")
    return "\n".join(lines)
