"""AnthropicCompatChat 协议适配测试(GLM coding 套餐 /api/anthropic 端点)。

2026-08-31:openai 协议 /coding/paas/v4 配额断供,用户切 Anthropic 协议端点
(Claude Code 同款)。协议差异各有断言:system 拎顶层、max_tokens 必填(65536,
思考强制开共享预算)、SSE 流式只拼 text_delta(thinking 块默认开;非流式
300s 读超时窗口装不下 65536 预算的长思考生成,实测反复 ReadTimeout 空转)。
"""

from __future__ import annotations

import json

import pytest

from edaloop.llm.base import ChatMessage
from edaloop.llm.openai_compat import AnthropicCompatChat, get_llm


class _StreamResp:
    """伪 SSE 流式响应:输入事件序列,逐行吐 data: 行。"""

    def __init__(self, events: list[dict], status: int = 200) -> None:
        self.status_code = status
        lines = []
        for e in events:
            lines.append(f"data: {json.dumps(e, ensure_ascii=False)}")
        self._lines = lines
        self.request = None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(f"{self.status_code}", request=None, response=self)

    def iter_lines(self):
        yield from self._lines


class _StreamCtx:
    def __init__(self, resp: _StreamResp) -> None:
        self._resp = resp

    def __enter__(self) -> _StreamResp:
        return self._resp

    def __exit__(self, *exc) -> None:
        return None


def _events_text(parts: list[str]) -> list[dict]:
    return ([{"type": "content_block_start", "index": 0,
              "content_block": {"type": "thinking"}}]
            + [{"type": "content_block_delta", "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "内心戏不算正文"}}]
            + [{"type": "content_block_delta", "index": 1,
                "delta": {"type": "text_delta", "text": t}} for t in parts]
            + [{"type": "message_delta", "delta": {"stop_reason": "end_turn"}}])


def _client() -> AnthropicCompatChat:
    return AnthropicCompatChat(base="https://open.bigmodel.cn/api/anthropic",
                               key="sk-test", model="glm-5.3")


def test_anthropic_system_lifted_and_text_deltas_joined(monkeypatch) -> None:
    seen: dict = {}

    def fake_stream(method, url, headers=None, json=None, timeout=None):  # noqa: A002
        seen.update(url=url, headers=headers, payload=json)
        return _StreamCtx(_StreamResp(_events_text(["前半", "后半"])))

    monkeypatch.setattr("edaloop.llm.openai_compat.httpx.stream", fake_stream)
    out = _client().chat([ChatMessage(role="system", content="你是规划器"),
                          ChatMessage(role="user", content="出计划")])
    assert out == "前半后半"
    assert seen["url"].endswith("/v1/messages")
    assert seen["headers"]["x-api-key"] == "sk-test"
    assert "anthropic-version" in seen["headers"]
    # system 拎成顶层参数;messages 只剩 user;流式+大预算(思考共享预算)
    assert seen["payload"]["system"] == "你是规划器"
    assert seen["payload"]["messages"] == [{"role": "user", "content": "出计划"}]
    assert seen["payload"]["max_tokens"] == 65536
    assert seen["payload"]["stream"] is True


def test_anthropic_consecutive_roles_merged(monkeypatch) -> None:
    seen: dict = {}

    def fake_stream(method, url, headers=None, json=None, timeout=None):  # noqa: A002
        seen.update(payload=json)
        return _StreamCtx(_StreamResp(_events_text(["ok"])))

    monkeypatch.setattr("edaloop.llm.openai_compat.httpx.stream", fake_stream)
    _client().chat([ChatMessage(role="user", content="问1"),
                    ChatMessage(role="user", content="问2"),
                    ChatMessage(role="assistant", content="答1"),
                    ChatMessage(role="user", content="问3")])
    msgs = seen["payload"]["messages"]
    assert msgs == [{"role": "user", "content": "问1\n\n问2"},
                    {"role": "assistant", "content": "答1"},
                    {"role": "user", "content": "问3"}]


def test_anthropic_no_text_delta_raises(monkeypatch) -> None:
    monkeypatch.setattr("edaloop.llm.openai_compat.time.sleep", lambda _s: None)
    monkeypatch.setattr(
        "edaloop.llm.openai_compat.httpx.stream",
        lambda *a, **k: _StreamCtx(_StreamResp(
            [{"type": "content_block_delta", "index": 0,
              "delta": {"type": "thinking_delta", "thinking": "只思考"}},
             {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}}])))
    with pytest.raises(RuntimeError, match="无 text 增量"):
        _client().chat([ChatMessage(role="user", content="x")])


def test_get_llm_routes_by_base(monkeypatch) -> None:
    monkeypatch.setenv("EDALOOP_LLM_KEY", "sk-test")
    monkeypatch.setenv("EDALOOP_LLM_BASE", "https://open.bigmodel.cn/api/anthropic")
    assert isinstance(get_llm(), AnthropicCompatChat)
    monkeypatch.setenv("EDALOOP_LLM_BASE", "https://open.bigmodel.cn/api/paas/v4")
    assert not isinstance(get_llm(), AnthropicCompatChat)
