"""聊天意图路由测试:快路径 / LLM 分诊 / 失败兜底(不依赖 chainlit,LLM 用桩)。"""

from __future__ import annotations

from edaloop.ui.router import classify, is_meta_question


class StubLLM:
    """chat() 依次吐预置回复;记调用数供断言。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0

    def chat(self, messages) -> str:  # noqa: ANN001 - 桩,签名宽松
        self.calls += 1
        return self.replies.pop(0)


class TestFastPath:
    def test_meta_short_questions(self):
        for t in ("你会做什么", "你能干嘛?", "你是谁", "怎么用", "有哪些功能", "能做什么"):
            assert is_meta_question(t), t

    def test_long_text_with_keyword_is_not_meta(self):
        # 含"你可以"的长需求不得被快路径吞掉(长度上限防误伤)
        t = "你可以给我设计一个 24V 转 5V 的电源板,带防反接和 USB 输出,另外要 RS485 接口"
        assert not is_meta_question(t)

    def test_fast_path_no_llm_needed(self):
        intent, reply = classify("你会做什么", llm=None)
        assert intent == "chat" and reply == ""  # 回复=能力卡,由展示层给


class TestLLMRouting:
    def test_requirement_passthrough(self):
        llm = StubLLM(['{"intent": "requirement", "reply": ""}'])
        intent, reply = classify("帮我画一个 ESP32 最小系统板", llm=llm)
        assert (intent, reply) == ("requirement", "")
        assert llm.calls == 1

    def test_question_with_reply(self):
        llm = StubLLM(['```json\n{"intent": "question", "reply": "我可以从需求生成原理图…"}\n```'])
        intent, reply = classify("你们生成的原理图能导出网表吗", llm=llm)
        assert intent == "chat" and "原理图" in reply

    def test_garbage_output_falls_back_to_requirement(self):
        llm = StubLLM(["我不是 JSON", "还是不合法 {{{"])
        intent, _ = classify("随便一句长文本,LLM 两次都坏", llm=llm)
        assert intent == "requirement"
        assert llm.calls == 2  # 重试一次后兜底

    def test_no_llm_long_text_is_requirement(self):
        intent, _ = classify("一个 12 页的长需求文档内容 " * 10, llm=None)
        assert intent == "requirement"
