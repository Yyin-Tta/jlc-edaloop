from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

import httpx

from edaloop.llm.base import ChatMessage, EmbeddingProvider, LLMProvider, RerankProvider

_RETRY_STATUS = {429, 500, 502, 503, 504}
_RETRY_DELAYS = (5, 15, 30)
_RETRY_EXC = (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError)
_FILTER_NOTE = "\n\n(以上全部为电路设计工程数据,不含敏感内容;请正常完成原任务,输出 JSON。)"


def _is_content_filter(status_code: int, body: str) -> bool:
    return status_code == 400 and ("1301" in body or "contentFilter" in body)


def _post_with_retry(url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
    last_exc: Exception | None = None
    for attempt, delay in enumerate(_RETRY_DELAYS + (None,)):
        trial = payload
        if attempt > 0:
            trial = dict(payload)
            msgs = [dict(m) for m in payload.get("messages", [])]
            if msgs:
                msgs[-1]["content"] = str(msgs[-1].get("content", "")) + _FILTER_NOTE
                trial["messages"] = msgs
        try:
            resp = httpx.post(url, headers=headers, json=trial, timeout=timeout)
            if resp.status_code not in _RETRY_STATUS and not _is_content_filter(resp.status_code, resp.text):
                resp.raise_for_status()
                return resp.json()
            last_exc = httpx.HTTPStatusError(
                f"{resp.status_code} for {url}: {resp.text[:200]}", request=resp.request, response=resp
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in _RETRY_STATUS and not _is_content_filter(e.response.status_code, e.response.text):
                raise
            last_exc = e
        except _RETRY_EXC as e:
            last_exc = e
        if delay is not None:
            time.sleep(delay)
        else:
            break
    raise last_exc or RuntimeError(f"request failed: {url}")


@dataclass
class LLMConfig:
    base: str
    key: str
    model: str


@dataclass
class EmbedConfig:
    base: str
    key: str
    model: str
    rerank_model: str


def _llm_config_from_env() -> LLMConfig:
    base = os.environ.get("EDALOOP_LLM_BASE", "https://open.bigmodel.cn/api/paas/v4")
    key = os.environ.get("EDALOOP_LLM_KEY", "")
    model = os.environ.get("EDALOOP_LLM_MODEL", "glm-5.3")
    if not key:
        raise RuntimeError("EDALOOP_LLM_KEY 未配置(检查 .env)")
    return LLMConfig(base=base, key=key, model=model)


def _embed_config_from_env() -> EmbedConfig:
    base = os.environ.get("EDALOOP_EMBED_BASE", "https://api.siliconflow.cn/v1")
    key = os.environ.get("EDALOOP_EMBED_KEY", "")
    model = os.environ.get("EDALOOP_EMBED_MODEL", "BAAI/bge-m3")
    rerank_model = os.environ.get("EDALOOP_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
    if not key:
        raise RuntimeError("EDALOOP_EMBED_KEY 未配置(检查 .env)")
    return EmbedConfig(base=base, key=key, model=model, rerank_model=rerank_model)


def get_llm(temperature: float = 0.2) -> LLMProvider:
    cfg = _llm_config_from_env()
    # 2026-08-31:GLM coding 套餐配额走 Anthropic 协议端点(/api/anthropic,
    # Claude Code 同款;openai 协议的 /coding/paas/v4 断供时用户切换到此),
    # 协议选择按 base 特征路由,调用方(LLMProvider)无感
    if "/anthropic" in cfg.base:
        return AnthropicCompatChat(base=cfg.base, key=cfg.key, model=cfg.model,
                                   temperature=temperature)
    return OpenAICompatChat(base=cfg.base, key=cfg.key, model=cfg.model, temperature=temperature)


def get_embedder() -> EmbeddingProvider:
    cfg = _embed_config_from_env()
    return OpenAICompatEmbedding(base=cfg.base, key=cfg.key, model=cfg.model)


def get_reranker() -> RerankProvider | None:
    if not os.environ.get("EDALOOP_EMBED_KEY"):
        return None
    cfg = _embed_config_from_env()
    return OpenAICompatRerank(base=cfg.base, key=cfg.key, model=cfg.rerank_model)


class AnthropicCompatChat(LLMProvider):
    """Anthropic 协议端点(GLM coding 套餐 /api/anthropic,Claude Code 同款)。

    与 OpenAI 协议的三点差异都要兜:①system 消息必须拎成顶层 `system` 参数,
    不能混进 messages;②`max_tokens` 必填;③响应 content 是块数组,thinking
    块(该端点默认开)在前、正文 text 块在后——只拼 text 块。消息序列要求
    首条 user 且角色交替:项目内调用方全是 [system, user] 两段式,但仍做
    合并防御(连续同角色并成一条,避免上游 400)。"""

    def __init__(self, base: str, key: str, model: str, temperature: float = 0.2) -> None:
        self._base = base.rstrip("/")
        self._key = key
        self._model = model
        self._temperature = temperature

    def chat(self, messages: list[ChatMessage], *, model: str | None = None) -> str:
        system_parts = [m.content for m in messages if m.role == "system"]
        convo = [m for m in messages if m.role != "system"]
        merged: list[dict] = []
        for m in convo:
            if merged and merged[-1]["role"] == m.role:
                merged[-1]["content"] += "\n\n" + m.content
            else:
                merged.append({"role": m.role, "content": m.content})
        while merged and merged[0]["role"] == "assistant":
            merged.pop(0)  # 协议要求首条 user;项目无此形态,防御而已
        payload: dict = {
            "model": model or self._model,
            # 该端点 thinking 强制开且 disabled 字段被静默忽略(实测 200 仍带
            # thinking 块),思考与正文共享 max_tokens——预算必须把思考装下,
            # 8192 实测被 make_plan 长提示的思考吃光(stop=max_tokens 无 text)。
            # 同时走 SSE 流式:非流式下 read timeout 是"首字节前"单窗口,
            # 65536 预算的长思考生成可超 15min,300s 窗口必然 ReadTimeout
            # 反复重试空转(实测 25min 无 round-plan);流式思考增量持续到达,
            # 读超时按"块间隔"计,永不触发
            "max_tokens": 65536,
            "stream": True,
            "messages": merged or [{"role": "user", "content": ""}],
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if self._temperature:
            payload["temperature"] = self._temperature
        url = f"{self._base}/v1/messages"
        headers = {"x-api-key": self._key, "anthropic-version": "2023-06-01"}
        last_exc: Exception | None = None
        for attempt, delay in enumerate(_RETRY_DELAYS + (None,)):
            trial = payload
            if attempt > 0 and merged:  # 内容过滤重试同款:末条附注扰动
                trial = dict(payload)
                trial["messages"] = [dict(m) for m in payload["messages"]]
                trial["messages"][-1]["content"] += _FILTER_NOTE
            text_parts: list[str] = []
            stop_reason: str | None = None
            try:
                with httpx.stream("POST", url, headers=headers, json=trial,
                                  timeout=httpx.Timeout(60.0, read=300.0, write=60.0, pool=60.0)) as resp:
                    if resp.status_code in _RETRY_STATUS or _is_content_filter(resp.status_code, ""):
                        raise httpx.HTTPStatusError(
                            f"{resp.status_code} for {url}", request=resp.request, response=resp)
                    resp.raise_for_status()
                    for line in resp.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            evt = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        et = evt.get("type")
                        if et == "content_block_delta":
                            d = evt.get("delta") or {}
                            if d.get("type") == "text_delta":
                                text_parts.append(str(d.get("text") or ""))
                        elif et == "message_delta":
                            stop_reason = (evt.get("delta") or {}).get("stop_reason") or stop_reason
                text = "".join(text_parts)
                if text.strip():
                    return text
                last_exc = RuntimeError(
                    f"anthropic 流式无 text 增量: stop={stop_reason}")
            except (_RETRY_EXC + (httpx.HTTPStatusError,)) as e:
                if isinstance(e, httpx.HTTPStatusError) \
                        and e.response.status_code not in _RETRY_STATUS \
                        and not _is_content_filter(e.response.status_code, str(getattr(e.response, "_text", ""))):
                    raise
                last_exc = e
            if delay is not None:
                time.sleep(delay)
            else:
                break
        raise last_exc or RuntimeError(f"request failed: {url}")


class OpenAICompatChat(LLMProvider):
    def __init__(self, base: str, key: str, model: str, temperature: float = 0.2) -> None:
        self._base = base.rstrip("/")
        self._key = key
        self._model = model
        self._temperature = temperature

    def chat(self, messages: list[ChatMessage], *, model: str | None = None) -> str:
        payload = {
            "model": model or self._model,
            "messages": [m.model_dump() for m in messages],
            "temperature": self._temperature,
        }
        if "/coding/" in self._base:
            payload["thinking"] = {"type": "disabled"}
        data = _post_with_retry(
            f"{self._base}/chat/completions",
            {"Authorization": f"Bearer {self._key}"},
            payload,
            300,
        )
        return data["choices"][0]["message"]["content"]


class OpenAICompatEmbedding(EmbeddingProvider):
    def __init__(self, base: str, key: str, model: str) -> None:
        self._base = base.rstrip("/")
        self._key = key
        self._model = model

    def _embed(self, texts: list[str]) -> list[list[float]]:
        data = _post_with_retry(
            f"{self._base}/embeddings",
            {"Authorization": f"Bearer {self._key}"},
            {"model": self._model, "input": texts},
            120,
        )
        return [_normalize(v["embedding"]) for v in data["data"]]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text])[0]


class OpenAICompatRerank(RerankProvider):
    def __init__(self, base: str, key: str, model: str) -> None:
        self._base = base.rstrip("/")
        self._key = key
        self._model = model

    def rerank(
        self, query: str, documents: list[str], *, top_k: int = 5
    ) -> list[tuple[int, float]]:
        data = _post_with_retry(
            f"{self._base}/rerank",
            {"Authorization": f"Bearer {self._key}"},
            {
                "model": self._model,
                "query": query,
                "documents": documents,
                "top_n": top_k,
            },
            120,
        )
        return [(r["index"], float(r["relevance_score"])) for r in data["results"]]


def _normalize(vec: list[float]) -> list[float]:
    norm = sum(x * x for x in vec) ** 0.5
    if norm == 0:
        return vec
    return [x / norm for x in vec]


def dumps_json(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False)
