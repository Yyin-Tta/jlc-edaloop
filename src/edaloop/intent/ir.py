from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Tolerant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _drop_unknown(cls, v):
        if isinstance(v, dict):
            return {k: val for k, val in v.items() if k in cls.model_fields}
        return v


class Spec(_Tolerant):
    """结构化约束(P4-0①):param/value/unit 可被校核器程序化消费,tolerance/source 留溯源。"""

    param: str
    value: str
    unit: str = ""
    tolerance: str | None = None
    source: str | None = None

    @field_validator("param", "value", "unit", "tolerance", "source", mode="before")
    @classmethod
    def _coerce_num_to_str(cls, v):
        # parse LLM 偶发吐 JSON 数值(value=5 而非 "5",daily req-03 实锤)——数值裹胁成字符串,
        # bool/None 原样走(既有类型校验兜底)。
        if isinstance(v, bool) or v is None:
            return v
        if isinstance(v, (int, float)):
            return f"{v:g}"
        return v


class Function(_Tolerant):
    name: str
    desc: str = ""
    constraints: list[Spec | str] = Field(default_factory=list)

    def constraints_digest(self) -> str:
        """constraints 统一转文本(结构化 Spec 展开 param=value+unit,自由文本原样)。"""
        out: list[str] = []
        for c in self.constraints:
            if isinstance(c, Spec):
                seg = f"{c.param}={c.value}{c.unit or ''}"
                if c.tolerance:
                    seg += f" ±{c.tolerance}"
                out.append(seg)
            else:
                out.append(c)
        return "; ".join(x for x in out if x)


class Interface(_Tolerant):
    type: str
    spec: str = ""


class PowerRail(_Tolerant):
    """v_min/v_max(P4-0①):宽压轨(锂电池 3.0-4.2V/USB 4.75-5.25V)不再硬塞标称值;
    source 记来源(USB-C/锂电池/DC 端子),供电校核用。"""

    name: str | None = None
    voltage: float | None = None
    v_min: float | None = None
    v_max: float | None = None
    imax: float | None = None
    source: str | None = None

    def v_text(self) -> str:
        """轨电压文本:标称 "3.3V" / 范围 "3.0-4.2V" / 无数值回退轨名(三态降级,不抛)。"""
        if self.voltage is not None:
            return f"{self.voltage:g}V"
        if self.v_min is not None or self.v_max is not None:
            lo = f"{self.v_min:g}" if self.v_min is not None else "?"
            hi = f"{self.v_max:g}" if self.v_max is not None else "?"
            return f"{lo}-{hi}V"
        return self.name or "?"


class Power(_Tolerant):
    inputs: list[str] = Field(default_factory=list)
    rails: list[PowerRail] = Field(default_factory=list)
    protection: str | None = None


class Env(_Tolerant):
    temp: str | None = None
    size: str | None = None
    cost_target: str | None = None
    fab: str | None = None


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
            rail = r.v_text()
            if r.imax is not None:
                rail += f" {r.imax:g}A"
            if r.name and r.name not in rail:
                rail += f" {r.name}"
            lines.append(rail)
        if self.power.protection:
            lines.append(self.power.protection)
        return "\n".join(lines)
