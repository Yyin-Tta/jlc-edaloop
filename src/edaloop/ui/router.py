"""聊天意图路由:用户文本在进 run 前先分诊(需求 vs 提问/闲聊)。

分层纪律同 session.py:纯逻辑可测,不 import chainlit。
- 快路径:能力/用法类短问(≤48 字符 + 关键词)零 LLM 直接判 chat,回复由展示层给能力卡;
- LLM 分诊:其余文本问一次 LLM(结构化 JSON 出 intent+reply);
- 兜底:LLM 缺失/输出不合法一律回 requirement——宁可错跑(报错有回显),不可吞需求。
带需求文件(.md/.txt)的消息不过这层(显式需求信号)。
"""

from __future__ import annotations

import json
import re

from edaloop.llm.base import ChatMessage

_META_FAST = re.compile(
    r"你会|你能|你可以|你支持|帮助|怎么用|怎么使用|怎么玩|什么功能|有哪些功能|"
    r"能干(什么|嘛|啥)|是干(什么|嘛|啥)的|介绍(一下)?(你|自己)|你是谁|能做(什么|嘛|啥)"
)

# 分诊器的人设与能力事实(回答 question 只准用这些;与 app._WELCOME 同源维护)
_SYSTEM = """你是 edaloop(嘉立创 EDA 原理图设计 agent)的聊天分诊器。判断用户这条消息是:
- requirement:一段硬件/电路设计需求(想设计一块板子,含"帮我画/设计/做一个…电路/板")
- question:其他一切(能力/用法咨询、领域知识提问、闲聊)

你自己的能力事实(回答 question 时只用这些,不知道就如实说,结尾引导对方发需求):
- 原理图全链路:自然语言需求 → 解析 → 检索电路块 → 规划落图 → 机械校验迭代(默认 dry-run 只规划不出图;真机落图会先向用户确认)
- 交付物:原理图 SVG、网表、BOM+成本估算、参数选值建议、设计审查报告
- PCB 编排:原理图同步到 PCB、自动布局布线、门禁检查(编排上游能力,EasyEDA 内完成)
- 报价:PCB 制板/SMT 贴片/元件三段报价与订单草稿(永不自动支付)
- datasheet PDF 入库:提取引脚表与电气参数,扩充知识库
- 设计有歧义或未覆盖时,逐条向用户提问,答案并入增量需求自动重跑
- 不做:KiCad 后端、自动支付、无人工确认的批量操作

只输出 JSON(不要多余文本):
{"intent": "requirement" 或 "question", "reply": "question 时给用户的中文回答,两三句,务实;requirement 时空串"}"""


def is_meta_question(text: str) -> bool:
    """能力/用法类短问:快路径判定(长度上限防误伤含关键词的真需求)。"""
    return len(text.strip()) <= 48 and bool(_META_FAST.search(text))


def classify(text: str, llm=None) -> tuple[str, str]:
    """路由一条用户文本 → ('requirement', '') 或 ('chat', reply)。

    reply 为空串时,展示层应回能力卡(欢迎页)。llm 为 None(无 key/初始化失败)
    时只剩快路径,其余全按需求处理。
    """
    if is_meta_question(text):
        return "chat", ""
    if llm is None:
        return "requirement", ""
    for _ in range(2):
        try:
            reply = llm.chat(
                [
                    ChatMessage(role="system", content=_SYSTEM),
                    ChatMessage(role="user", content=text[:2000]),
                ]
            )
            raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", reply.strip()).strip()
            data = json.loads(raw)
        except Exception:  # noqa: BLE001 - 分诊失败不拦需求链路
            continue
        intent = str(data.get("intent", "")).lower()
        if intent in ("question", "chat"):
            return "chat", str(data.get("reply", "")).strip()
        if intent == "requirement":
            return "requirement", ""
    return "requirement", ""
