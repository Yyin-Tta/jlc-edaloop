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
    try:
        r = httpx.get(_API, params={"productCode": lcsc}, headers=_HEADERS, timeout=timeout)
        data = r.json()
    except Exception as e:
        pc.error = f"{type(e).__name__}: {e}"[:120]
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


def summarize_bom(
    blocks: list[dict],
    *,
    per_part_qty: int = 1,
) -> dict:
    """BlockPlan.blocks(或同构 dict)→ BOM 成本汇总。

    blocks 元素需含:instance, block_id;可选 lcsc(缺则计 unknown)。
    返回:总成本(有价件求和)/缺价清单/缺货清单/明细。
    """
    details: list[dict] = []
    total = 0.0
    priced = 0
    no_price: list[str] = []
    no_stock: list[str] = []
    seen: dict[str, int] = {}
    for b in blocks:
        lcsc = b.get("lcsc") or ""
        key = lcsc or b.get("block_id", "?")
        seen[key] = seen.get(key, 0) + per_part_qty
    for key, qty in seen.items():
        if not key.startswith("C"):
            no_price.append(f"{key}(无 C 号)")
            details.append({"ref": key, "qty": qty, "price": None, "note": "no-lcsc"})
            continue
        pc = fetch_costs([key])[key]
        if pc.error or pc.price is None:
            no_price.append(f"{key}({pc.error or 'no price'})")
            details.append({"ref": key, "qty": qty, "price": None, "note": pc.error})
            continue
        line = pc.price * qty
        total += line
        priced += 1
        if (pc.stock or 0) < qty:
            no_stock.append(f"{key}(stock={pc.stock})")
        details.append({"ref": key, "qty": qty, "unit": pc.price, "line": round(line, 4), "stock": pc.stock})
    return {
        "total": round(total, 4),
        "priced_lines": priced,
        "no_price": no_price,
        "no_stock": no_stock,
        "details": details,
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
