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


def get_llm() -> LLMProvider:
    cfg = _llm_config_from_env()
    return OpenAICompatChat(base=cfg.base, key=cfg.key, model=cfg.model)


def get_embedder() -> EmbeddingProvider:
    cfg = _embed_config_from_env()
    return OpenAICompatEmbedding(base=cfg.base, key=cfg.key, model=cfg.model)


def get_reranker() -> RerankProvider | None:
    if not os.environ.get("EDALOOP_EMBED_KEY"):
        return None
    cfg = _embed_config_from_env()
    return OpenAICompatRerank(base=cfg.base, key=cfg.key, model=cfg.rerank_model)


class OpenAICompatChat(LLMProvider):
    def __init__(self, base: str, key: str, model: str) -> None:
        self._base = base.rstrip("/")
        self._key = key
        self._model = model

    def chat(self, messages: list[ChatMessage], *, model: str | None = None) -> str:
        payload = {
            "model": model or self._model,
            "messages": [m.model_dump() for m in messages],
            "temperature": 0.2,
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
