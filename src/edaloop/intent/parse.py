from __future__ import annotations

import json
import re

from pydantic import ValidationError

from edaloop.intent.ir import DesignIR
from edaloop.llm.base import ChatMessage, LLMProvider

_SYSTEM = """你是硬件需求解析器。把客户需求文档解析为 DesignIR JSON,只输出 JSON,不要输出其他内容。
schema:
{
  "functions":   [{"name": "...", "desc": "...", "constraints": ["..."]}],
  "interfaces":  [{"type": "usb-c|uart|spi|i2c|gpio|rs485|...", "spec": "..."}],
  "power": {
    "inputs":    ["USB-C 5V", "DC 12-24V 端子", ...],
    "rails":     [{"name": "3V3", "voltage": 3.3, "imax": 0.5}],
    "protection": "TVS+自恢复保险丝" 或 null
  },
  "env":         {"temp": "-40~85C", "size": "50x50mm", "cost_target": null},
  "open_questions": [{"id": "Q1", "question": "...", "options": ["A", "B"]}]
}
规则:
- voltage/imax 是数值,单位 V/A;文字性输入(宽压范围)放 inputs
- 客户明确点名的器件/芯片名必须保留在 functions.desc 或 constraints 里
- 需求有歧义或二选一未定时,生成 open_questions,不要擅自决定
- 没有的字段给空数组/null"""


class IRParseError(Exception):
    def __init__(self, reason: str, raw: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.raw = raw


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t)
    return t.strip()


def requirement_to_ir(md_text: str, llm: LLMProvider, *, source: str = "requirement.md") -> DesignIR:
    reply = llm.chat(
        [
            ChatMessage(role="system", content=_SYSTEM),
            ChatMessage(role="user", content=md_text),
        ]
    )
    raw = _strip_fences(reply)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise IRParseError(f"LLM 输出不是合法 JSON: {e}", raw=raw) from e
    data.setdefault("source", source)
    try:
        return DesignIR.model_validate(data)
    except ValidationError as e:
        raise IRParseError(f"DesignIR 校验失败: {e}", raw=raw) from e
