from __future__ import annotations

import json
import re

from pydantic import ValidationError

from edaloop.ingest.models import PinInfo, PinTable
from edaloop.ingest.pdf_pages import rule_extract, rule_extract_bare
from edaloop.llm.base import ChatMessage, LLMProvider

_SYSTEM = """你是 datasheet 引脚表提取器。从给定的 PDF 文本(引脚定义页)提取引脚表 JSON,只输出 JSON。
schema:
{
  "part": "器件型号(从文本推断,如 ULN2003A)",
  "pins": [{"number": "1", "name": "1B", "io_type": "I", "desc": "..."}]
}
规则:
- number 是字符串引脚号;name 照抄原文(如 1B/1C/COM/E)
- io_type 只用 I/O/I/O/P/S/空字符串(原文为 — 时给空)
- 合并单元格(TI 风格:1B..7B 共享一句描述)要展开到每个引脚
- 不要发明引脚;原文有多少个引脚就提取多少个"""


class ExtractError(Exception):
    pass


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t)
    return t.strip()


def llm_extract(text: str, pdf_name: str, llm: LLMProvider, page_no: int, *, attempts: int = 3) -> PinTable:
    last: Exception | None = None
    for _ in range(attempts):
        reply = llm.chat(
            [
                ChatMessage(role="system", content=_SYSTEM),
                ChatMessage(role="user", content=f"PDF: {pdf_name} 页 {page_no}\n\n{text[:9000]}"),
            ]
        )
        raw = _strip_fences(reply)
        try:
            data = json.loads(raw)
            pins = [
                PinInfo.model_validate({**p, "page": page_no, "channel": "llm"})
                for p in data.get("pins", [])
            ]
            return PinTable(
                part=data.get("part", pdf_name),
                source_pdf=pdf_name,
                pages=[page_no],
                pins=pins,
            )
        except (json.JSONDecodeError, ValidationError) as e:
            last = ExtractError(f"LLM 引脚表输出无效: {e}")
    raise last if last else ExtractError("llm_extract failed")


_SUGGEST_SYSTEM = """你是 datasheet 外围电路建议提取器。从给定文本中提取对原理图设计有执行价值的建议,只输出 JSON 数组:
[{"kind": "decoupling|pull-up|series|protection|layout|other", "text": "建议(中文,含参数值)", "quote": "原文摘录(<=80字符)"}]
只收录原文明确写出的具体建议(带数值:电容容值/电阻阻值/走线要求);泛泛而谈的不要;没有则输出 []"""


def llm_extract_suggestions(
    text: str, llm: LLMProvider, page_no: int, *, attempts: int = 2
) -> list[dict]:
    last: Exception | None = None
    for _ in range(attempts):
        reply = llm.chat(
            [
                ChatMessage(role="system", content=_SUGGEST_SYSTEM),
                ChatMessage(role="user", content=text[:9000]),
            ]
        )
        raw = _strip_fences(reply)
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [
                    {"kind": str(s.get("kind", "other")), "text": str(s.get("text", ""))[:200], "quote": str(s.get("quote", ""))[:100], "page": page_no}
                    for s in data
                    if s.get("text")
                ]
        except json.JSONDecodeError as e:
            last = ExtractError(f"建议提取输出无效: {e}")
    if last:
        raise last
    return []


def rule_channel(text: str, page_no: int) -> list[PinInfo]:
    pins = rule_extract(text, page_no)
    if len(pins) < 4:
        pins = rule_extract_bare(text, page_no)
    return [PinInfo.model_validate(p) for p in pins]
