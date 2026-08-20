"""自由拓扑模式库(Phase 2-A):已知功能→器件组合的确定性分解。

设计原则(ADR-0008):
- 模式命中 → 确定性生成 PlannedBlock 级联(不依赖 LLM 拓扑决策);
- 无模式 → 留给 planner 的 LLM 分解(受 ≤5 器件与 pinout 校验约束);
- 模拟反馈拓扑(运放增益网络/补偿网络)永远不在此处。
"""

from __future__ import annotations

from edaloop.generate.models import PlannedBlock
from edaloop.knowledge.models import RetrievedBlock

# 每个模式: 功能关键词(匹配 DesignIR functions/需求文本) → 器件 block_id + 引脚绑定模板
# 网名约定:功能内部网用 <prefix>_<net>,对外端口显式列出(power/gnd 汇入全局轨)
PATTERNS: list[dict] = [
    {
        "id": "liion-protection",
        "keywords": ["锂电保护", "过充", "过放", "过流保护", "battery protection", "保护板"],
        "parts": [
            {
                "block_id": "battery-dw01-protection",
                "suffix": "dw01",
                "pins": {
                    "1": "{p}_OD",  # OD -> FS G1
                    "2": "{p}_CSI",
                    "3": "{p}_OC",
                    "5": "{p}_VDD",
                    "6": "GND",
                },
            },
            {
                "block_id": "mos-fs8205a-dual",
                "suffix": "fs",
                "pins": {
                    "1": "GND",
                    "2": "GND",
                    "3": "{p}_BMINUS",
                    "4": "{p}_OC",
                    "5": "GND",
                    "6": "{p}_OD",
                },
            },
        ],
        "notes": "DW01A VDD 经 100R 接 B+,CSI 经 1k 接 B-(阻容属外围,弱门禁提示)",
    },
    {
        "id": "can-node",
        "keywords": ["can 收发", "canbus", "can 2.0", "can 总线", "can 节点"],
        "parts": [
            {
                "block_id": "can-tja1051",
                "suffix": "can",
                "pins": {
                    "1": "{p}_TXD",
                    "2": "GND",
                    "3": "5V",
                    "4": "{p}_RXD",
                    "7": "{p}_CANL",
                    "8": "{p}_CANH",
                },
            }
        ],
        "notes": "CANH/CANL 差分对(120R 终端可选);5V 侧或 SN65HVD230 3V3 侧按电源域选块",
    },
]


def match_pattern(text: str) -> dict | None:
    """需求/IR 文本 → 命中的第一个模式(确定性)。"""
    low = text.lower()
    for pat in PATTERNS:
        if any(k in low for k in pat["keywords"]):
            return pat
    return None


def decompose(
    pattern: dict,
    candidates: dict[str, RetrievedBlock],
    prefix: str,
) -> tuple[list[PlannedBlock], list[str]]:
    """模式 → PlannedBlock 列表(直接进 plan.blocks)+ uncovered 提示。"""
    blocks: list[PlannedBlock] = []
    notes = [pattern.get("notes", "")]
    for part in pattern["parts"]:
        cand = candidates.get(part["block_id"])
        if cand is None or not cand.lcsc:
            return [], [f"模式 {pattern['id']} 缺原料器件 {part['block_id']}(检索未命中)"]
        pins = {no: tmpl.format(p=prefix) for no, tmpl in part["pins"].items()}
        blocks.append(
            PlannedBlock(
                block_id=part["block_id"],
                upstream_id="",
                instance=f"{prefix}_{part['suffix']}",
                pins_binding=pins,
                provenance=f"自由拓扑模式 {pattern['id']}(确定性分解)",
            )
        )
    return blocks, [n for n in notes if n]
