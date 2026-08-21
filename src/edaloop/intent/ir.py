from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Tolerant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _drop_unknown(cls, v):
        if isinstance(v, dict):
            return {k: val for k, val in v.items() if k in cls.model_fields}
        return v


class Function(_Tolerant):
    name: str
    desc: str = ""
    constraints: list[str] = Field(default_factory=list)


class Interface(_Tolerant):
    type: str
    spec: str = ""


class PowerRail(_Tolerant):
    name: str | None = None
    voltage: float
    imax: float | None = None


class Power(_Tolerant):
    inputs: list[str] = Field(default_factory=list)
    rails: list[PowerRail] = Field(default_factory=list)
    protection: str | None = None


class Env(_Tolerant):
    temp: str | None = None
    size: str | None = None
    cost_target: str | None = None


class OpenQuestion(_Tolerant):
    id: str
    question: str
    options: list[str] = Field(default_factory=list)
    answer: str = ""


class DesignIR(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    source: str
    created: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    functions: list[Function] = Field(default_factory=list)
    interfaces: list[Interface] = Field(default_factory=list)
    power: Power = Field(default_factory=Power)
    env: Env = Field(default_factory=Env)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    revision: int = 1
    decisions: dict[str, str] = Field(default_factory=dict)

    def apply_answers(self, answers: dict[str, str]) -> int:
        """把 {Q-id: 答案文本} 应用到 open_questions;已答的从列表移除。

        返回应用数;revision 自增(审计留痕 IR-v1→v2)。
        答案同时以 decisions 字段保留(planner/refine 可消费结构化决策)。
        """
        applied = 0
        remaining = []
        decided: dict[str, str] = dict(self.decisions or {})
        for q in self.open_questions:
            if q.id in answers and answers[q.id]:
                applied += 1
                decided[q.id] = answers[q.id]
            else:
                remaining.append(q)
        if applied:
            self.open_questions = remaining
            self.decisions = decided
            self.revision += 1
        return applied

    def decisions_digest(self) -> str:
        return "\n".join(f"[{k}] {v}" for k, v in sorted((self.decisions or {}).items()))

    def query_text(self) -> str:
        lines: list[str] = []
        for f in self.functions:
            lines.append(" ".join(x for x in [f.name, f.desc] if x))
        for i in self.interfaces:
            lines.append(" ".join(x for x in [i.type, i.spec] if x))
        for p in self.power.inputs:
            lines.append(p)
        for r in self.power.rails:
            rail = f"{r.voltage:g}V"
            if r.imax is not None:
                rail += f" {r.imax:g}A"
            if r.name:
                rail += f" {r.name}"
            lines.append(rail)
        if self.power.protection:
            lines.append(self.power.protection)
        return "\n".join(lines)
