"""P2-C BOM 成本通道(ADR-0008 C 项)。

数据流:块库 C 号 → LCSC 实时价格/库存(wmsc ftps API) →
  ①检索层无侵入(不加权,避免价格波动污染检索);
  ②规划层成本提示:等价类内给 planner 价格对比表;
  ③交付层 BOM 成本汇总(delivery.bom.json)。

原则:价格数据只做提示与汇总(弱信号),不做选型强门禁——库存/价格时效性
由调用点实时查询保证,不缓存长期(ADR-0008)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

_API = "https://wmsc.lcsc.com/ftps/wm/product/detail"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
}

# P5-4④ 豁免口径(§5.1):缺价行分「豁免」(数据源限制,单列报告不进有价分母)与
# 「真缺」(补录/重试目标,进分母)。豁免两类:
#   - C99xx 延展号段:基础库未挂商务数据,wmsc 无价是数据源限制而非缺陷;
#   - std 无值件(resistor-std/capacitor-std):值在 sizing 时定,块级无固定 C 号。
_C99_PREFIX = "C99"
_STD_VALUE_BLOCKS = frozenset({"resistor-std", "capacitor-std"})


@dataclass
class PartCost:
    lcsc: str
    price: float | None = None
    stock: int | None = None
    moq: int | None = None
    error: str = ""


def fetch_cost(lcsc: str, *, timeout: float = 15.0) -> PartCost:
    """单器件实时价格/库存。失败返回带 error 的 PartCost(不抛,弱信号)。"""
    pc = PartCost(lcsc=lcsc)
    if not lcsc or not lcsc.upper().startswith("C"):
        pc.error = "invalid lcsc"
        return pc
    # P5-4②:SSL/连接瞬态重试一次(历史 run 实测 6 行缺价全是 ConnectError 瞬态;
    # 弱信号不抛原则不变,两次都败才落 error)
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            r = httpx.get(_API, params={"productCode": lcsc}, headers=_HEADERS, timeout=timeout)
            data = r.json()
            last_exc = None
            break
        except Exception as e:
            last_exc = e
            continue
    if last_exc is not None:
        pc.error = f"{type(last_exc).__name__}: {last_exc}"[:120]
        return pc
    if not data.get("ok"):
        pc.error = f"api not ok: {str(data.get('code'))[:40]}"
        return pc
    res = data.get("result") or {}
    prices = res.get("productPriceList") or []
    if prices:
        tier = sorted(prices, key=lambda p: int(p.get("ladder", 999)) or 999)[0]
        try:
            pc.price = float(tier.get("currencyPrice", 0) or 0)
        except (TypeError, ValueError):
            pass
        pc.moq = tier.get("ladder")
    try:
        pc.stock = int(res.get("stockNumber", 0) or 0)
    except (TypeError, ValueError):
        pass
    if pc.price is None and pc.stock is None:
        pc.error = "no price/stock fields"
    elif pc.price is None:
        pc.error = pc.error or "api ok but no price (C99xx 延展号段常见,基础库未挂商务数据)"
    return pc


def fetch_costs(lcscs: list[str]) -> dict[str, PartCost]:
    return {c: fetch_cost(c) for c in lcscs}


def exempt_reason(lcsc: str, block_id: str = "") -> str:
    """缺价行豁免归类(§5.1 P5-4④)。

    返回豁免原因('exempt-c99xx'/'exempt-std'),或 '' 表示真缺(no-lcsc/no-price,
    是补录或重试目标,进有价分母)。仅对「无价」行调用;有价行不进此判。
    """
    if not lcsc or not lcsc.upper().startswith("C"):
        return "exempt-std" if block_id in _STD_VALUE_BLOCKS else ""
    if lcsc.upper().startswith(_C99_PREFIX):
        return "exempt-c99xx"
    return ""


def summarize_bom(
    blocks: list[dict],
    *,
    per_part_qty: int = 1,
) -> dict:
    """BlockPlan.blocks(或同构 dict)→ BOM 成本汇总。

    blocks 元素需含:instance, block_id;可选 lcsc(缺则计 unknown)。
    返回:总成本(有价件求和)/缺价清单/缺货清单/明细 + coverage(非豁免行有价覆盖率,
    豁免口径见 exempt_reason)。
    """
    details: list[dict] = []
    total = 0.0
    priced = 0
    no_price: list[str] = []
    no_stock: list[str] = []
    seen: dict[str, dict] = {}
    for b in blocks:
        lcsc = b.get("lcsc") or ""
        key = lcsc or b.get("block_id", "?")
        if key in seen:
            seen[key]["qty"] += per_part_qty
        else:
            seen[key] = {"qty": per_part_qty, "block_id": b.get("block_id", "?")}
    exempt = {"exempt-c99xx": 0, "exempt-std": 0}
    for key, item in seen.items():
        qty = item["qty"]
        block_id = item["block_id"]
        if not key.startswith("C"):
            reason = exempt_reason("", block_id)
            note = "std 无值件(值在 sizing 定,块级无固定 C 号)" if reason else "no-lcsc"
            if reason:
                exempt[reason] += 1
            no_price.append(f"{key}({note})")
            details.append({"ref": key, "qty": qty, "price": None, "note": note})
            continue
        pc = fetch_costs([key])[key]
        if pc.error or pc.price is None:
            reason = exempt_reason(key)
            note = pc.error or ("C99xx 延展号段无商务数据(数据源限制)" if reason else "no price")
            if reason:
                exempt[reason] += 1
            no_price.append(f"{key}({note})")
            details.append({"ref": key, "qty": qty, "price": None, "note": note})
            continue
        line = pc.price * qty
        total += line
        priced += 1
        if (pc.stock or 0) < qty:
            no_stock.append(f"{key}(stock={pc.stock})")
        details.append({"ref": key, "qty": qty, "unit": pc.price, "line": round(line, 4), "stock": pc.stock})
    total_lines = len(seen)
    exempt_lines = sum(exempt.values())
    non_exempt = total_lines - exempt_lines
    return {
        "total": round(total, 4),
        "priced_lines": priced,
        "no_price": no_price,
        "no_stock": no_stock,
        "details": details,
        "coverage": {
            "total_lines": total_lines,
            "exempt_lines": exempt_lines,
            "exempt_c99xx": exempt["exempt-c99xx"],
            "exempt_std": exempt["exempt-std"],
            "non_exempt_lines": non_exempt,
            "priced_lines": priced,
            "coverage": round(priced / non_exempt, 4) if non_exempt else 1.0,
        },
    }


def cost_hint_for_planner(
    groups: dict[str, list[dict]],
) -> str:
    """等价类 → planner 成本提示文本(检索层无侵入)。

    groups: {功能名: [{block_id, lcsc, ...}]} — 同功能可互换块。
    """
    lines = []
    for fn, parts in groups.items():
        if len(parts) < 2:
            continue
        costs = fetch_costs([p["lcsc"] for p in parts if p.get("lcsc")])
        seg = [f"{p['block_id']}({p.get('lcsc','-')}): " + (f"¥{costs[p['lcsc']].price}" if p.get("lcsc") and costs[p["lcsc"]].price is not None else "无价") for p in parts]
        lines.append(f"[成本对比:{fn}] " + " vs ".join(seg) + "(价格实时,仅参考;若无成本诉求忽略)")
    return "\n".join(lines)
