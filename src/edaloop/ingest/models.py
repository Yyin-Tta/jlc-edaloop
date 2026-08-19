from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PinInfo(_Strict):
    number: str
    name: str
    io_type: str = ""
    desc: str = ""
    page: int = 0
    channel: str = ""
    agreed: bool = True


class PinTable(_Strict):
    part: str
    source_pdf: str
    pages: list[int] = Field(default_factory=list)
    pins: list[PinInfo] = Field(default_factory=list)


class Suggestion(_Strict):
    text: str
    page: int = 0
    quote: str = ""
    kind: str = "general"


class IngestReport(_Strict):
    part: str
    pdf: str
    pin_count: int
    evidence_pages: list[int]
    llm_pins: int
    rule_pins: int
    disagreements: list[str] = Field(default_factory=list)
    internal_violations: list[str] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(default_factory=list)
    verdict: str = "fail"

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"
