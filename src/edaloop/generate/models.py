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
    no_connect: list[str] = Field(default_factory=list)
    params: dict[str, str] = Field(default_factory=dict)
    provenance: str = ""
    at: str = ""
    zone: str = ""
    page: str = ""  # compile 页流分配的落图页(P1..Pn);planner 不给,由布局定
    module: str = ""  # 功能模块名(小写短词,如 mcu/power/motor1/usb-serial);装箱亲和用,缺省按带序

    @model_validator(mode="before")
    @classmethod
    def _drop_unknown_keys(cls, v: Any) -> Any:
        if isinstance(v, dict):
            known = {
                "block_id", "upstream_id", "instance", "ports_binding", "pins_binding",
                "no_connect", "no_connect_pins", "params", "provenance", "at", "zone", "page", "module",
            }
            out = {k: val for k, val in v.items() if k in known and k != "no_connect_pins"}
            if "no_connect" not in out and "no_connect_pins" in v:
                out["no_connect"] = v["no_connect_pins"]
            return out
        return v

    @field_validator("params", mode="before")
    @classmethod
    def _coerce_params(cls, v: Any) -> dict[str, str]:
        if not isinstance(v, dict):
            return {}
        return {str(k): (val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)) for k, val in v.items()}

    @field_validator("no_connect", mode="before")
    @classmethod
    def _coerce_no_connect(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, (list, tuple, set)):
            return []
        return list(dict.fromkeys(str(pin).strip() for pin in v if str(pin).strip()))

    @model_validator(mode="after")
    def _validate_no_connect(self) -> "PlannedBlock":
        overlap = set(self.no_connect) & set(self.pins_binding)
        if overlap:
            raise ValueError(f"引脚同时出现在 pins_binding 与 no_connect: {sorted(overlap)}")
        return self


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
    page: str = ""  # 落图页(P1 = 工程首页免建;P2+ 由 controller 建页并 --doc 钉扎)
