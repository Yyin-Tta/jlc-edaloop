from __future__ import annotations

from pydantic import BaseModel, Field


class PartRef(BaseModel):
    ref: str
    lcsc: str | None = None
    note: str = ""


class BlockRecord(BaseModel):
    block_id: str
    name: str
    desc: str
    category: str = "general"
    tags: list[str] = Field(default_factory=list)
    parts: list[PartRef] = Field(default_factory=list)
    ports: list[str] = Field(default_factory=list)
    provenance: str = ""


class RetrievedBlock(BaseModel):
    block_id: str
    name: str
    desc: str
    category: str
    tags: list[str]
    parts: list[PartRef]
    ports: list[str]
    provenance: str
    score: float
    channels: list[str]
    rank: int = 0
