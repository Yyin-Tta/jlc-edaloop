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
    # P4-0 器件参数槽(开放键值,P4-4 sizing 消费):vf/if(LED)/f_sw(开关电源)/rja(热阻)/
    # vref(基准)。键名小写惯例;值一律字符串(数值由消费方解析);出处并入 source。
    params: dict[str, str] = Field(default_factory=dict)


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


class CaseDigest(BaseModel):
    """P4-6③:案例/IR 结构化相似度指纹——轨集合+接口类型+功能名,三组加权 Jaccard。

    只放可从 DesignIR 机械抽出的字段(无 LLM 二次加工),保证回写与查询两侧同源。
    """

    rails: list[str] = Field(default_factory=list)  # 归一轨 token:"3v3"/"5v"/"12v"
    interfaces: list[str] = Field(default_factory=list)  # 接口类型小写:"rs485"/"usb-c"
    functions: list[str] = Field(default_factory=list)  # 功能名小写:"主控"/"电源监测"


class CaseRecord(BaseModel):
    """P4-6③:案例库记录——一次 PASS run 的 (IR 指纹 → 整组块) 映射。

    回写三护栏:eval 源不写(pipeline 层拒)、origin 标记溯源、hash 去重(digest+blocks 规范化)。
    """

    case_id: str
    name: str
    origin: str  # "run:<ir.id>" | "seed"
    digest: CaseDigest = Field(default_factory=CaseDigest)
    block_ids: list[str] = Field(default_factory=list)
    created: str = ""
