from __future__ import annotations

from pydantic import BaseModel, Field


class PartRef(BaseModel):
    ref: str
    lcsc: str | None = None
    note: str = ""
    params: dict[str, str] = Field(default_factory=dict)


class Electrical(BaseModel):
    """块级电气摘要(P4-0②):供电轨兼容范围/电流预算,F4 供电校核消费。

    字段由 wmsc paramVOList 回填(knowledge/electrical.py 品类映射)或 datasheet 摘录写入;
    全部可缺省——缺数据的块按三态降级报 UNKNOWN,不静默放行也不误杀。
    """

    v_supply_min: float | None = None
    v_supply_max: float | None = None
    i_max: float | None = None
    i_typ: float | None = None
    rails: list[str] = Field(default_factory=list)
    source: str = ""


class UpstreamRef(BaseModel):
    id: str
    ports: dict[str, str] = Field(default_factory=dict)
    status: str = ""


class BlockRecord(BaseModel):
    block_id: str
    name: str
    desc: str
    category: str = "general"
    tags: list[str] = Field(default_factory=list)
    parts: list[PartRef] = Field(default_factory=list)
    ports: list[str] = Field(default_factory=list)
    provenance: str = ""
    upstream: UpstreamRef | None = None
    lcsc: str | None = None
    pinout: dict[str, str] | None = None
    electrical: Electrical | None = None


class RetrievedBlock(BaseModel):
    block_id: str
    name: str
    desc: str
    category: str
    tags: list[str]
    parts: list[PartRef]
    ports: list[str]
    provenance: str
    upstream: UpstreamRef | None = None
    lcsc: str | None = None
    pinout: dict[str, str] | None = None
    electrical: Electrical | None = None
    score: float
    channels: list[str]
    rank: int = 0
