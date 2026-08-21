from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlannedBlock(_Strict):
    block_id: str
    upstream_id: str = ""
    instance: str
    ports_binding: dict[str, str] = Field(default_factory=dict)
    pins_binding: dict[str, str] = Field(default_factory=dict)
    params: dict[str, str] = Field(default_factory=dict)
    provenance: str = ""
    at: str = ""
    zone: str = ""

    @model_validator(mode="before")
    @classmethod
    def _drop_unknown_keys(cls, v: Any) -> Any:
        if isinstance(v, dict):
            known = {
                "block_id", "upstream_id", "instance", "ports_binding", "pins_binding",
                "params", "provenance", "at", "zone",
            }
            return {k: val for k, val in v.items() if k in known}
        return v

    @field_validator("params", mode="before")
    @classmethod
    def _coerce_params(cls, v: Any) -> dict[str, str]:
        if not isinstance(v, dict):
            return {}
        return {str(k): (val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)) for k, val in v.items()}


class NetDecl(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    net_class: str = Field(default="signal", alias="class")


class BlockPlan(_Strict):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    design_ir_id: str = ""
    source: str = ""
    created: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    blocks: list[PlannedBlock] = Field(default_factory=list)
    nets: list[NetDecl] = Field(default_factory=list)
    uncovered: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    provenance: list[str] = Field(default_factory=list)

    @field_validator("uncovered", "provenance", mode="before")
    @classmethod
    def _coerce_str_lists(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            return [v] if v.strip() else []
        if v is None:
            return []
        return [str(x) for x in v]


class Action(_Strict):
    kind: str
    block_instance: str = ""
    upstream_id: str = ""
    lcsc: str = ""
    mpn: str = ""
    args: list[str] = Field(default_factory=list)
    desc: str = ""
    pinout: dict[str, str] | None = None
    zone: str = ""
