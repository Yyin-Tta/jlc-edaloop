"""P4-4② 标准件表查表:value → LCSC C 号(确定性选值落图的查表层)。

表 = seeds/standard-parts.json(源头:easyeda-agent 社区 curated 表,导入纪律见表内 _doc)。
本模块只做三件事:
  - canon_value:人写值归一到表键口径("330Ω"/"330R"→"330","100nF"→"100n","4u7"→"4.7u","10K"→"10k");
  - lookup:精确命中(确定性:不猜最近值——表里没有的值由 planner 换值,不静默替换);
  - kind_of:tags 含 std-value 的块 → resistor/capacitor 通道判别。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

_STD_TAG = "std-value"
# block_id → 表段名(新增 std-value 块需在此登记)
_KIND_OF_BLOCK = {"resistor-std": "resistor", "capacitor-std": "capacitor"}


def _table_path() -> Path:
    here = Path(__file__).resolve()
    for cand in (Path("seeds/standard-parts.json"), here.parents[2] / "seeds" / "standard-parts.json"):
        if cand.exists():
            return cand
    raise FileNotFoundError("seeds/standard-parts.json 不存在(标准件表是 std-value 通道的硬依赖)")


@lru_cache(maxsize=1)
def load_table() -> dict[str, dict]:
    data = json.loads(_table_path().read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if k in ("resistor", "capacitor")}


def kind_of(rec) -> str | None:
    """BlockRecord/RetrievedBlock → 'resistor'|'capacitor'|None(std-value 块判别)。"""
    bid = getattr(rec, "block_id", "") or ""
    if bid in _KIND_OF_BLOCK:
        return _KIND_OF_BLOCK[bid]
    tags = [str(t).lower() for t in (getattr(rec, "tags", None) or [])]
    if _STD_TAG in tags:
        for t in tags:
            if t in ("resistor", "capacitor"):
                return t
    return None


def canon_value(kind: str, text: str) -> str:
    """人写值 → 表键。电阻:_fmt_ohm 口径;电容:µF→u/nF→n/pF→p。支持 4u7/4k7 码值。"""
    t = str(text or "").strip()
    if kind == "resistor":
        t = t.replace("Ω", "").replace("R", "", 1)
    t = t.replace("µ", "u").replace("μ", "u").replace("F", "").replace("f", "")
    m = re.fullmatch(r"(\d+)(?:\.(\d+))?\s*([kKmMuUnNpP]?)(\d*)", t)
    if not m:
        return t
    ip, fp, unit, tail = m.group(1), m.group(2) or "", m.group(3), m.group(4)
    if tail:  # 4u7/42k2 码值:尾段是小数首位串
        fp = tail + fp
    num = float(f"{ip}.{fp}") if fp else float(ip)
    if kind == "resistor":
        if unit in ("k", "K"):
            num *= 1e3
        elif unit == "M":
            num *= 1e6
        elif unit == "m":
            num *= 1e-3
        if num >= 1e6:
            return f"{num / 1e6:g}M"
        if num >= 1e3:
            return f"{num / 1e3:g}k"
        return f"{num:g}"
    # capacitor:表键 u/n/p
    u = unit.lower()
    if u == "u":
        num *= 1e-6
    elif u == "n":
        num *= 1e-9
    elif u == "p":
        num *= 1e-12
    if num >= 1e-6:
        return f"{num / 1e-6:g}u"
    if num >= 1e-9:
        return f"{num / 1e-9:g}n"
    return f"{num / 1e-12:g}p"


def lookup(kind: str, value: str) -> dict | None:
    """精确查表(归一后);未命中返回 None——不猜最近值,由 planner 显式换值。"""
    table = load_table()
    seg = table.get(kind) or {}
    return seg.get(canon_value(kind, value))


def available_values(kind: str, limit: int = 24) -> list[str]:
    seg = (load_table().get(kind) or {})
    return sorted(seg, key=lambda v: _numish(v))[:limit]


def _numish(v: str) -> float:
    try:
        return float(re.sub(r"[A-Za-z]", "", v) or 0)
    except ValueError:
        return 0.0
