from __future__ import annotations

import json
import re

from pydantic import ValidationError

from edaloop.generate.models import BlockPlan
from edaloop.intent.ir import DesignIR
from edaloop.knowledge.models import RetrievedBlock
from edaloop.llm.base import ChatMessage, LLMProvider


class PlanError(Exception):
    def __init__(self, reason: str, raw: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.raw = raw


_SYSTEM = """你是电路块规划器。给定 DesignIR(设计意图)和候选块目录,产出 BlockPlan JSON,只输出 JSON。

输出 schema:
{
  "blocks": [
    {"block_id": "...", "upstream_id": "block.xxx", "instance": "唯一实例名",
     "ports_binding": {"PORT名": "网络名"}, "pins_binding": {}, "params": {}, "provenance": "检索分数或选择理由",
     "zone": "left|center|right|left-top|center-bottom|...(可选)",
     "at": "x,y(可选,页内锚点,仅 RELAYOUT 反馈指定空位时给)",
     "params": {"spacing": "250-600(可选,块内格距覆盖,仅 RELAYOUT 反馈要求时给)"}
  ],
  "nets": [{"name": "3V3", "class": "power"}],
  "uncovered": ["<目录无法覆盖的功能,简述>"],
  "confidence": 0.0-1.0,
  "provenance": ["整体理由"]
}

规则:
- 只能使用目录中带 upstream 字段的块(block-apply 通道:照抄 upstream.id,端口绑定用 ports_binding)或带 lcsc+pinout 的库外器件(place 通道:upstream_id 留空,逐引脚绑定用 pins_binding,key 是 pinout 里的引脚号),或带 std_value 的标准 R/C 通道(params.value 必须取目录里列出的可用值之一,pins_binding 用引脚号 1/2)
- 标准 R/C(resistor-std/capacitor-std):采纳 sizing 建议值或 datasheet 惯例值时用,params.value 给表内标准值(如 "330"/"10k"/"100n"/"22u"),instance 用 r*/c* 前缀短名;值必须有出处(反馈里的 sizing 推荐值/需求原文),不要发明阻容值
- place 通道的未用引脚一律不绑(pins_binding 省略该键),不要发明 NC 网络去接闲置脚
- **自由拓扑分解**:某功能目录无整块时,若能由目录中 ≤5 个带 lcsc+pinout 的器件组合实现(如"锂电保护"=DW01A+FS8205A),则为每个器件生成一个 place 通道实例(instance 加功能后缀如 prot_dw01/prot_fs),用 pins_binding 接线实现互联;器件间互联靠相同网名(与块通道同规则);不可行或需要运放反馈/补偿类模拟拓扑时仍列入 uncovered
- 目录覆盖不了的功能逐条列入 uncovered(不要静默省略,也不要发明器件);provenance 只写整体理由
- 覆盖 DesignIR 的 functions/power/interfaces;目录中无对应块的功能,跳过并在 provenance 里注明"未覆盖:<功能>"
- 电源网络命名:3V3/5V/GND;信号网络用大写下划线(MCU_TX/RS485_DE 等)
- 端口绑定必须逐块完整:每个 PORT 都要给 net;块间互联靠相同 net 名汇合(如 LDO 的 3V3 口与 MCU 的 3V3 口都绑 "3V3")
- 串口交叉:USB 串口块的 TXD 与主控 RX 同网,RXD 与主控 TX 同网
- 需要多个相同块(如多路 LED)时,生成多个实例,instance 命名不同(led1/led2)
- zone 可选:布局分区提示(左=电源入口,中=主控,右=接口外设);不填按块品类默认,仅当有明确布局理由(如客户点名"USB 口放右边")才给
- at/params.spacing 可选且仅在上一轮 RELAYOUT 反馈点名时给:at 是页内锚点(A4 画布 1170×825,x∈[100,1100] y∈[300,780]),params.spacing 覆盖该块默认格距 250;布局由编译器自动分页,planner 不要发明页号
- 器件型号以目录 parts 为准,不要发明器件"""


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t)
    return t.strip()


def ensure_std_candidates(candidates: list, catalog: dict | None) -> list:
    """P4-4②:std R/C 通道常驻候选。

    planner 提示词承诺了 resistor-std/capacitor-std 选值通道;若检索没召回这两个块,
    planner 按规则用它们反而被「目录外块」校验击杀、耗尽重试后整轮 ERROR(req-11 实证)。
    把缺的 std 块从全量 catalog 补进候选(make_plan 只读字段名,BlockRecord 可直接并入)。
    """
    if not catalog:
        return list(candidates)
    have = {getattr(c, "block_id", "") for c in candidates}
    extra = [catalog[b] for b in ("resistor-std", "capacitor-std") if b in catalog and b not in have]
    return list(candidates) + extra


def make_plan(
    ir: DesignIR,
    candidates: list[RetrievedBlock],
    llm: LLMProvider,
    *,
    attempts: int = 3,
    feedback: str = "",
    cost_hint: str = "",
    answer_context: str = "",
) -> BlockPlan:
    """LLM 规划 → 校验(目录外/upstream/引脚/表内值),校验失败带原因重试(attempts 内收敛)。"""
    from edaloop.generate.stdparts import available_values, kind_of, lookup

    appliable = [b for b in candidates if b.parts and b.block_id]
    catalog_lines = []
    for b in candidates:
        std_kind = None
        if b.upstream is None and not b.lcsc:
            # P4-4② std-value 通道:resistor-std/capacitor-std(值查标准件表落图)
            std_kind = kind_of(b)
            if std_kind is None:
                continue
        entry = {
            "block_id": b.block_id,
            "name": b.name,
            "desc": b.desc,
            "parts": [p.ref for p in b.parts],
        }
        if b.category:
            entry["category"] = b.category
        if b.upstream:
            entry["upstream"] = {"id": b.upstream.id, "ports": dict(b.upstream.ports)}
        if b.lcsc:
            entry["lcsc"] = b.lcsc
        if std_kind is not None:
            entry["std_value"] = {"kind": std_kind, "values": available_values(std_kind)}
        if b.pinout:
            entry["pinout"] = dict(b.pinout)
        catalog_lines.append(json.dumps(entry, ensure_ascii=False))
    user = (
        "DesignIR:\n"
        + json.dumps(json.loads(ir.model_dump_json()), ensure_ascii=False, indent=1)
    )
    digest = ir.decisions_digest()
    if digest:
        user += "\n\n已确认决策(必须落实到 blocks 选择:如决策选了双电源方案,就要选出对应电源块):\n" + digest
    user += "\n\n候选块目录:\n" + "\n".join(catalog_lines)
    if feedback:
        user += (
            "\n\n上一轮验证反馈(修正要求,优先级高于你的默认选择):\n" + feedback
        )
    if answer_context:
        user += answer_context
    if cost_hint:
        user += "\n\n成本参考(实时价格,仅当需求有成本诉求时参考,不要为省钱牺牲功能):\n" + cost_hint
    last: Exception | None = None
    for _ in range(attempts):
        ask = user
        if last is not None:  # P4-4②残留:校验拒绝要带原因重问,否则同提示词重试必然同错(req-11 实证)
            ask = user + "\n\n上一次 plan 被校验拒绝,原因:\n" + str(last) + "\n修正这个问题后重新输出完整 plan JSON。"
        reply = llm.chat(
            [
                ChatMessage(role="system", content=_SYSTEM),
                ChatMessage(role="user", content=ask),
            ]
        )
        raw = _strip_fences(reply)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            last = PlanError(f"planner 输出不是合法 JSON: {e}", raw=raw)
            continue
        try:
            plan = BlockPlan.model_validate(
                {"design_ir_id": ir.id, "source": ir.source, **data}
            )
        except ValidationError as e:
            last = PlanError(f"BlockPlan 校验失败: {e}", raw=raw)
            continue
        valid_ids = {b.block_id: (b.upstream.id if b.upstream else "") for b in candidates}
        bad = [b.block_id for b in plan.blocks if b.block_id not in valid_ids]
        if bad:
            last = PlanError(f"plan 引用了目录外的块: {bad}", raw=raw)
            continue
        mismatched = [
            (b.block_id, b.upstream_id, want)
            for b in plan.blocks
            if (want := valid_ids.get(b.block_id)) and b.upstream_id != want
        ]
        if mismatched:
            last = PlanError(f"upstream_id 与目录不一致(照抄目录,不要截断): {mismatched[:3]}", raw=raw)
            continue
        lcsc_pinouts = {
            b.block_id: (b.lcsc, b.pinout or {})
            for b in candidates
            if b.lcsc and b.pinout
        }
        bad_place = []
        std_kinds = {
            b.block_id: kind_of(b)
            for b in candidates
            if b.upstream is None and not b.lcsc and (kind_of(b) is not None)
        }
        for b in plan.blocks:
            if b.upstream_id:
                continue
            std_kind = std_kinds.get(b.block_id)
            if std_kind is not None:
                val = (b.params or {}).get("value", "")
                if not val:
                    bad_place.append(f"{b.block_id}: 缺 params.value(标准 R/C 必须给表内值)")
                    continue
                if lookup(std_kind, val) is None:
                    bad_place.append(
                        f"{b.block_id}: 值 {val!r} 不在标准件表,可用如 {available_values(std_kind, 12)}"
                    )
                unknown_pins = [p for p in b.pins_binding if p not in ("1", "2")]
                if unknown_pins:
                    bad_place.append(f"{b.block_id}: 标准件引脚号只有 1/2,给了 {unknown_pins[:4]}")
                continue
            lcsc, pinout = lcsc_pinouts.get(b.block_id, ("", {}))
            if not lcsc:
                bad_place.append(f"{b.block_id}: 无 lcsc+pinout,不可 place")
                continue
            unknown_pins = [p for p in b.pins_binding if p not in pinout]
            if unknown_pins:
                bad_place.append(f"{b.block_id}: 引脚号 {unknown_pins[:4]} 不在 pinout {list(pinout)[:6]}")
        if bad_place:
            last = PlanError("place 通道块校验失败(引脚号必须来自目录 pinout): " + "; ".join(bad_place[:3]), raw=raw)
            continue
        return plan
    raise last if last else PlanError("planner 失败")
