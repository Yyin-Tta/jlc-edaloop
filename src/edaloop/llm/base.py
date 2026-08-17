from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class LLMProvider(ABC):
    """文本 LLM 抽象:DeepSeek 或任意 OpenAI 兼容端点(含本地 vLLM/Ollama)。"""

    @abstractmethod
    def chat(self, messages: list[ChatMessage], *, model: str | None = None) -> str: ...


class EmbeddingProvider(ABC):
    """embedding 抽象:PoC 走硅基流动 BGE-M3,Phase 1 落本地权重(ADR-0006)。"""

    dim: int = 1024

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    @abstractmethod
    def embed_query(self, text: str) -> list[float]: ...


class RerankProvider(ABC):
    """rerank 抽象:BGE-reranker-v2-m3(ADR-0006)。"""

    @abstractmethod
    def rerank(
        self, query: str, documents: list[str], *, top_k: int = 5
    ) -> list[tuple[int, float]]:
        """返回 (documents 索引, 分数) 列表,按分数降序,截取 top_k。"""
        ...
