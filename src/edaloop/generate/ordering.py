"""P3-6 下单通道 M9(ADR-0009 §P3-6)。

纪律(R12 资金安全):
- 默认止步**报价单**;订单草稿仅 `--confirm` 显式生成;
- 自动支付**永不做**(§3 非目标);
- JLC 下单 API 无公开契约(R13):制造文件走 EasyEDA 客户端下单(人工导出 gerber),
  本模块产出报价单 + 预检报告 + 下单指引清单。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from edaloop.generate.adapter import EasyedaAdapter
from edaloop.generate.bomcost import fetch_costs

# JLC 经济板报价参数(2026-08 公开价参考,订单时以官方页为准)
_PCB_TIER_2L = {"5pcs": 2.0, "10pcs": 5.0, "30pcs": 12.0, "100pcs": 38.0}
_SMT_SETUP = {"economic": 50.0}  # 经济 SMT 上料费/面(基库免上料活动常驻,以官方为准)
_SMT_PER_JOINT = 0.005  # 每焊点贴片费(经济档参考值)


@dataclass
class Quote:
    pcb_qty: int = 5
    pcb_cost: float = 0.0
    smt_cost: float = 0.0
    parts_cost: float = 0.0
    parts_missing: list[str] = field(default_factory=list)
    total: float = 0.0
    notes: list[str] = field(default_factory=list)


def precheck_bom(bom_path: str | Path) -> dict:
    """SMT 兼容预检:全件有 C 号/库存≥qty/MOQ 达标;不达标列替代建议位。"""
    bom = json.loads(Path(bom_path).read_text(encoding="utf-8"))
    problems = []
    lcscs = [d.get("ref") for d in bom.get("details", []) if str(d.get("ref", "")).startswith("C")]
    costs = fetch_costs(lcscs)
    for det in bom.get("details", []):
        ref = det.get("ref", "")
        qty = det.get("qty", 1)
        if not str(ref).startswith("C"):
            problems.append({"ref": ref, "issue": "no-lcsc", "fix": "补 C 号或改手工焊接"})
            continue
        pc = costs.get(ref)
        if pc is None or pc.price is None:
            problems.append({"ref": ref, "issue": "no-price", "fix": "C99xx 延展号段,确认基础库可贴或换料"})
            continue
        if (pc.stock or 0) < qty:
            problems.append({"ref": ref, "issue": f"stock={pc.stock}<{qty}", "fix": "换等价有货料(见 swap 报告)或等补货"})
        if pc.moq and qty < int(pc.moq or 0):
            problems.append({"ref": ref, "issue": f"qty={qty}<MOQ={pc.moq}", "fix": "数量提到 MOQ 或换料"})
    return {"ok": not problems, "problems": problems}


def quote(
    bom_path: str | Path,
    *,
    layers: int = 2,
    qty: int = 5,
    joints: int | None = None,
) -> Quote:
    """三段报价:PCB 制板 + SMT 贴片 + 元件。"""
    q = Quote(pcb_qty=qty)
    if layers == 2:
        tier = min(_PCB_TIER_2L, key=lambda k: abs(int(k.replace("pcs", "")) - qty))
        q.pcb_cost = _PCB_TIER_2L[tier]
    else:
        q.pcb_cost = 30.0 + 0.6 * qty
        q.notes.append("4 层板报价为估算,以官方计价器为准")
    bom = json.loads(Path(bom_path).read_text(encoding="utf-8"))
    parts_total = float(bom.get("total", 0) or 0)
    q.parts_cost = parts_total
    est_joints = joints if joints is not None else sum(d.get("qty", 1) * 4 for d in bom.get("details", []))
    q.smt_cost = _SMT_SETUP["economic"] + est_joints * _SMT_PER_JOINT
    q.notes.append(f"焊点按估算 {est_joints}(原理图 pins×4 粗估);实际以贴片计算为准")
    q.total = q.pcb_cost + q.smt_cost + q.parts_cost
    q.notes.append("PCB/SMT 为经济档参考价,下单以嘉立创官方页实时价为准")
    return q


def order_draft(quote_: Quote, project: str, *, out_dir: str | Path) -> Path:
    """订单草稿(--confirm 后才调用):内容=规格+报价+人工下单步骤,绝不含支付动作。"""
    out = Path(out_dir) / "order-draft.md"
    lines = [
        "# 订单草稿(未提交,无支付)",
        f"项目: {project} | 数量: {quote_.pcb_qty}",
        "",
        "## 报价明细",
        f"- PCB 制板: ¥{quote_.pcb_cost:.2f}",
        f"- SMT 贴片: ¥{quote_.smt_cost:.2f}",
        f"- 元件(BOM 实价): ¥{quote_.parts_cost:.2f}",
        f"- **合计: ¥{quote_.total:.2f}**",
        "",
        "## 人工下单步骤",
        "1. EasyEDA Pro 打开工程 → 制造 → PCB 制板(检查层数/数量与此单一致)",
        "2. SMT 贴片:上传同目录 BOM(带 C 号)与坐标;核对预检报告问题项",
        "3. 提交前核对报价与本文差异;支付在官方页面人工完成",
        "",
        "## 备注",
        *[f"- {n}" for n in quote_.notes],
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def export_fab_package(adapter: EasyedaAdapter, out_dir: str | Path) -> dict:
    """制造文件包:PCB 几何 dump + 快照(gerber 由客户端下单流程生成,R13 兜底)。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    arts = {}
    try:
        dump = adapter.run_json(["pcb", "dump"])
        (out / "pcb-geometry.json").write_text(json.dumps(dump, ensure_ascii=False), encoding="utf-8")
        arts["geometry"] = str(out / "pcb-geometry.json")
    except Exception as e:
        arts["geometry_error"] = str(e)[:120]
    rc, outpng, _ = adapter.run(["pcb", "snapshot"])
    if rc == 0:
        arts["snapshot_stdout"] = outpng[:200]
    return arts
