from __future__ import annotations

import hashlib

from edaloop.llm.base import ChatMessage, EmbeddingProvider, LLMProvider, RerankProvider


def _trigrams(text: str) -> list[str]:
    t = text.lower()
    return [t[i : i + 3] for i in range(max(len(t) - 2, 0))]


def _bag(text: str, dim: int) -> list[float]:
    vec = [0.0] * dim
    for g in _trigrams(text):
        vec[int(hashlib.md5(g.encode()).hexdigest()[:8], 16) % dim] += 1.0
    norm = sum(x * x for x in vec) ** 0.5
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


class FakeEmbedding(EmbeddingProvider):
    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return _bag(text, self.dim)


class FakeRerank(RerankProvider):
    def rerank(
        self, query: str, documents: list[str], *, top_k: int = 5
    ) -> list[tuple[int, float]]:
        q = set(_trigrams(query))
        scored = []
        for i, doc in enumerate(documents):
            d = set(_trigrams(doc))
            overlap = len(q & d) / len(q) if q else 0.0
            scored.append((i, overlap))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]


class FakeChat(LLMProvider):
    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.messages: list[list[ChatMessage]] = []

    def chat(self, messages: list[ChatMessage], *, model: str | None = None) -> str:
        self.messages.append(messages)
        return self._reply
