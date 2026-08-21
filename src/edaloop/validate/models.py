from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Where(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str = ""
    net: str = ""
    pin: str = ""
    xy: str = ""


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    where: Where = Field(default_factory=Where)
    evidence: str = ""
    severity: str = "error"
    suggested_fix_class: str = ""
    weak: bool = False

    def key(self) -> str:
        return f"{self.code}|{self.where.ref}|{self.where.net}|{self.where.pin}|{self.evidence[:80]}"


STRONG_BLOCKING = ("GATE_FAIL", "MISSING_RAIL", "PIN_MISMATCH")


def is_blocking(f: Finding) -> bool:
    if f.weak:
        return False
    return f.code in STRONG_BLOCKING or f.severity == "error"
