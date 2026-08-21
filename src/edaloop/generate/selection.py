"""P3-2 选型升级:替代料 swap 提案 + SMT 可制造性标注(ADR-0009 §P3-2)。

数据通道(对齐 ADR-0004 分工 + R13 兜底):
- 价格/库存:LCSC wmsc API(已验证免鉴权);
- SMT 库类型:JLC selectSmtComponentList 无公开契约(域名不可达,R13 触发)——
  以 LCSC 数据近似:基础库判定走 C 号段启发 + wmsc 返回的 assemblyProcess/
  productAttribute 字段(有则用,无则 unknown,诚实降级)。

swap 语义:等价类内(同功能同规格)块 A↔块 B 三维对比(价/库存/库类型),
产出弱门禁提案(确认后人来按,不自动改 plan)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from edaloop.generate.bomcost import PartCost, fetch_costs


@dataclass
class SwapProposal:
    function: str
    from_block: str
    to_block: str
    from_lcsc: str
    to_lcsc: str
    from_cost: PartCost
    to_cost: PartCost
    saving_pct: float = 0.0

    def render(self) -> str:
        def _c(pc: PartCost) -> str:
            return f"¥{pc.price}(存{pc.stock})" if pc.price is not None else "无价"

        return (
            f"[swap:{self.function}] {self.from_block}({self.from_lcsc}, {_c(self.from_cost)})"
            f" → {self.to_block}({self.to_lcsc}, {_c(self.to_cost)})"
            f" | 预计降本 {self.saving_pct:.1f}%"
        )


def smt_library_type(lcsc: str, *, timeout: float = 12.0) -> str:
    """SMT 库类型近似判定:JLC API 无公开契约(R13),按 LCSC 属性诚实降级。

    还认:basic / extended / unknown。
    启发:C 号数值越大越可能是扩展库(基础库多为早期分配的短号);
    wmsc 的 productAttribute 里有 componentLibraryType 字段时以其为准。
    """
    import httpx

    if not lcsc or not lcsc.upper().startswith("C"):
        return "unknown"
    try:
        r = httpx.get(
            "https://wmsc.lcsc.com/ftps/wm/product/detail",
            params={"productCode": lcsc},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=timeout,
        )
        res = (r.json().get("result") or {})
        t = res.get("componentLibraryType") or res.get("productAttribute", {}).get("componentLibraryType") if isinstance(res.get("productAttribute"), dict) else res.get("componentLibraryType")
        if t:
            return str(t).lower()
    except Exception:
        pass
    try:
        num = int(lcsc[1:])
        return "basic" if num < 100000 else "extended"
    except ValueError:
        return "unknown"


def annotate_smt(lcscs: list[str]) -> dict[str, str]:
    return {c: smt_library_type(c) for c in lcscs}


def propose_swaps(
    groups: dict[str, list[dict]],
    *,
    require_price: bool = True,
) -> list[SwapProposal]:
    """等价类组 → swap 提案(每组的最低价件替换当前件,若更便宜)。

    groups: {功能名: [{block_id, lcsc}, ...]}(≥2 件才可能提案)。
    """
    proposals: list[SwapProposal] = []
    for fn, parts in groups.items():
        priced = [p for p in parts if p.get("lcsc")]
        if len(priced) < 2:
            continue
        costs = fetch_costs([p["lcsc"] for p in priced])
        entries = [(p, costs[p["lcsc"]]) for p in priced]
        valid = [(p, c) for p, c in entries if c.price is not None]
        if len(valid) < 2:
            if require_price:
                continue
        if len(valid) < 2:
            continue
        valid.sort(key=lambda pc: pc[1].price)
        cheapest_p, cheapest_c = valid[0]
        for p, c in valid[1:]:
            if c.price and cheapest_c.price and cheapest_c.price < c.price:
                saving = (c.price - cheapest_c.price) / c.price * 100
                if saving >= 5.0:
                    proposals.append(
                        SwapProposal(
                            function=fn,
                            from_block=p["block_id"],
                            to_block=cheapest_p["block_id"],
                            from_lcsc=p["lcsc"],
                            to_lcsc=cheapest_p["lcsc"],
                            from_cost=c,
                            to_cost=cheapest_c,
                            saving_pct=saving,
                        )
                    )
    return proposals


def proposals_report(proposals: list[SwapProposal]) -> str:
    if not proposals:
        return "(无 swap 提案:等价类内无 ≥5% 降本空间或数据不足)"
    lines = ["替代料提案(弱门禁:确认后人工换用,不自动改图):"]
    lines += [f"  {p.render()}" for p in proposals]
    return "\n".join(lines)


def dumps_groups(groups: dict[str, list[dict]]) -> str:
    return json.dumps(groups, ensure_ascii=False)
