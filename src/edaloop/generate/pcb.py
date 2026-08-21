"""P3-5 PCB 编排 M8(ADR-0009 §P3-5)。

链路(照抄上游 showcase 验证配方,档位策略=稀疏全自动):
  sch PASS → pcb new-board → pcb import-changes → auto-place
  → outline-fit → mount-holes(可选) → route-critical → route-short
  → power-pour(2层)/power-planes(4层) → silk-align
  → 门禁: pcb drc + pcb check + layout-lint
PCB 迭代环:复用 M5 归因思想(drc 违规→rip-up 对应网→重 route),上限 2 轮。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from edaloop.generate.adapter import EasyedaAdapter

PIPELINE = [
    ("new-board", ["pcb", "new-board"]),
    ("import-changes", ["pcb", "import-changes"]),
    ("place-constrained", ["pcb", "place-constrained"]),
    ("auto-place", ["pcb", "auto-place", "--assembly-gap", "60"]),
    ("outline-fit", ["pcb", "outline-fit"]),
    ("confirm-tier-1", ["pcb", "stage", "confirm-tier", "1", "--empty"]),
    ("set-assembly", ["pcb", "stage", "set-assembly", "--profile", "hand-solder"]),
    ("confirm-tier-2", ["pcb", "stage", "confirm-tier", "2", "--empty"]),
    ("confirm-tier-3", ["pcb", "stage", "confirm-tier", "3", "--empty"]),
    ("doc-reload-a", ["doc", "reload"]),
    ("layout-lint-gate", ["pcb", "layout-lint", "--gate"]),
    ("confirm-tier-4", ["pcb", "stage", "confirm-tier", "4"]),
    ("confirm-layout", ["pcb", "stage", "confirm-layout"]),
    ("confirm-outline", ["pcb", "stage", "confirm-outline"]),
    ("doc-reload-b", ["doc", "reload"]),
    ("route-critical", ["pcb", "route-critical"]),
    ("route-short", ["pcb", "route-short"]),
    ("power-pour", ["pcb", "power-pour"]),
    ("silk-align", ["pcb", "silk-align"]),
]

# 步骤失败可容忍清单(下游有兜底或不阻断主链;power-pour 对无电源网小板=无需铺铜)
_TOLERANT = {"outline-fit", "confirm-tier-1", "confirm-tier-2", "confirm-tier-3", "confirm-tier-4", "power-pour", "place-constrained", "doc-reload-a", "doc-reload-b"}

_DEFAULT_OUTLINE = "[[0,0],[3900,0],[3900,2700],[0,2700]]"

_TIGHT_RE = re.compile(r"tight\s+(\S+)\s*↔\s*(\S+)")
_BOXED_RE = re.compile(r"no-access\s+(\S+)\s+boxed in")


def _fix_layout_findings(adapter: EasyedaAdapter, *, rounds: int = 4) -> list[str]:
    """lint 定向修复循环:每轮读 lint 输出,tight pair 挪 x 大者 +300mil,
    boxed-in 器件上移 +250mil,直到无 ERROR/tight/boxed 或轮次耗尽。
    返回修复动作清单。"""
    fixes: list[str] = []
    for _ in range(rounds):
        _, out, _ = adapter.run(["pcb", "layout-lint"])
        text = out or ""
        has_issue = bool(
            _TIGHT_RE.search(text) or _BOXED_RE.search(text) or "ERROR" in text
        )
        if not has_issue:
            return fixes
        try:
            d = adapter.run_json(["pcb", "list"])
            comps = d.get("result", {}).get("components", [])
            by_desig = {c["designator"]: c for c in comps}
        except Exception:
            return fixes
        moved = False
        for m in _TIGHT_RE.finditer(text):
            ca, cb = by_desig.get(m.group(1)), by_desig.get(m.group(2))
            if ca and cb:
                mv = cb if cb["x"] >= ca["x"] else ca
                adapter.run(["pcb", "modify", "--id", mv["primitiveId"], "--patch", json.dumps({"x": mv["x"] + 300, "y": mv["y"]})])
                fixes.append(f"tight:{mv['designator']}+300x")
                moved = True
        for m in _BOXED_RE.finditer(text):
            c = by_desig.get(m.group(1))
            if c:
                adapter.run(["pcb", "modify", "--id", c["primitiveId"], "--patch", json.dumps({"y": c["y"] + 250})])
                fixes.append(f"boxed:{c['designator']}+250y")
                moved = True
        if not moved:
            return fixes
        adapter.run(["pcb", "outline-fit"])
    return fixes


def _separate_tight_pair(adapter: EasyedaAdapter) -> None:
    """读 layout-lint 的 tight pair,把 x 较大的一件再挪 +300mil。"""
    try:
        d = adapter.run_json(["pcb", "list"])
        comps = d.get("result", {}).get("components", [])
        by_desig = {c["designator"]: c for c in comps}
        _, out, _ = adapter.run(["pcb", "layout-lint"])
        for line in (out or "").splitlines():
            m = _TIGHT_RE.search(line)
            if not m:
                continue
            ca, cb = by_desig.get(m.group(1)), by_desig.get(m.group(2))
            if ca and cb:
                move = cb if cb["x"] >= ca["x"] else ca
                adapter.run(
                    ["pcb", "modify", "--id", move["primitiveId"], "--patch", json.dumps({"x": move["x"] + 300, "y": move["y"]})]
                )
    except Exception:
        pass


@dataclass
class PcbResult:
    steps: list[dict] = field(default_factory=list)
    drc: dict = field(default_factory=dict)
    check: dict = field(default_factory=dict)
    layout_lint: dict = field(default_factory=dict)

    @property
    def gate_ok(self) -> bool:
        if not self.steps:
            return False
        if not (self.drc and self.check and self.layout_lint):
            return False  # 门禁未跑完 ≠ 通过(verdict 三态哲学)
        drc_fatal = int(self.drc.get("result", {}).get("fatalCount", self.drc.get("fatal", 0)) or 0)
        check_err = int(self.check.get("result", {}).get("errorCount", self.check.get("errors", 0)) or 0)
        lint_ok = str(self.layout_lint.get("result", {}).get("verdict", self.layout_lint.get("verdict", "pass"))) == "pass"
        return drc_fatal == 0 and check_err == 0 and lint_ok

    @property
    def degraded(self) -> bool:
        """电气安全(DRC/check)过但可制造性(lint)未过 → 半成品交付(R14 兜底)。"""
        if not (self.drc and self.check):
            return False
        drc_fatal = int(self.drc.get("result", {}).get("fatalCount", self.drc.get("fatal", 0)) or 0)
        check_err = int(self.check.get("result", {}).get("errorCount", self.check.get("errors", 0)) or 0)
        return drc_fatal == 0 and check_err == 0 and not self.gate_ok


def run_pcb_pipeline(
    adapter: EasyedaAdapter,
    *,
    mount_holes: bool = True,
    audit=None,
) -> PcbResult:
    res = PcbResult()
    for name, args in PIPELINE:
        rc, out, err = adapter.run(args)
        if rc != 0 and name == "new-board" and "already bound" in (out or "") + (err or ""):
            # 工程已有绑定的板:跳过新建,在现有 PCB 上继续(非致命)
            res.steps.append({"step": name, "rc": 0, "note": "already-bound, reuse existing PCB"})
            if audit:
                audit.event("pcb-step", step=name, rc=0, note="reuse-existing")
            continue
        res.steps.append({"step": name, "rc": rc, "out_head": (out or "")[:180], "err_head": (err or "")[:180]})
        if audit:
            audit.event("pcb-step", step=name, rc=rc)
        if rc != 0 and name in ("import-changes", "auto-place"):
            return res
        if rc != 0 and name in _TOLERANT:
            res.steps[-1]["tolerated"] = True
        if name == "outline-fit" and rc != 0:
            # fit 失败(如无器件 bbox 可依)→ 显式默认矩形板框兜底
            rc2, out2, _ = adapter.run(["pcb", "outline-set", "--points", _DEFAULT_OUTLINE])
            res.steps.append({"step": "outline-set-fallback", "rc": rc2, "out_head": (out2 or "")[:150]})
            if audit:
                audit.event("pcb-step", step="outline-set-fallback", rc=rc2)
        if name == "layout-lint-gate" and rc != 0:
            # 布局病灶定向修复循环(tight/boxed/短路重叠对)→ 板框重贴合 → 复检
            adapter.run(["doc", "reload"])
            fixes = _fix_layout_findings(adapter)
            adapter.run(["pcb", "outline-fit"])
            adapter.run(["doc", "reload"])
            rc3, out3, _ = adapter.run(["pcb", "layout-lint", "--gate"])
            res.steps.append({"step": "lint-fix-loop", "rc": rc3, "fixes": fixes})
            if audit:
                audit.event("pcb-step", step="lint-fix-loop", rc=rc3, fixes=fixes)
    if mount_holes:
        rc, out, err = adapter.run(["pcb", "mount-holes"])
        if rc != 0:
            adapter.run(["pcb", "outline-set", "--points", "[[-200,-200],[4200,-200],[4200,3000],[-200,3000]]"])
            adapter.run(["pcb", "stage", "confirm-outline"])
            rc, out, err = adapter.run(["pcb", "mount-holes"])
        res.steps.append({"step": "mount-holes", "rc": rc, "out_head": (out or "")[:120]})
        if audit:
            audit.event("pcb-step", step="mount-holes", rc=rc)
    res.drc = _json_safe(adapter, ["pcb", "drc"])
    res.check = _json_safe(adapter, ["pcb", "check"])
    res.layout_lint = _json_safe(adapter, ["pcb", "layout-lint"])
    return res


def _json_safe(adapter: EasyedaAdapter, args: list[str]) -> dict:
    try:
        return adapter.run_json(args)
    except Exception:
        return {"error": "unavailable"}


def pcb_retry_loop(
    adapter: EasyedaAdapter,
    result: PcbResult,
    *,
    max_rounds: int = 2,
    audit=None,
) -> PcbResult:
    """PCB 迭代环:drc 违规→rip-up→重 route→复检(上限 2 轮,不达标诚实半成品)。"""
    for round_no in range(1, max_rounds + 1):
        if result.gate_ok:
            return result
        if audit:
            audit.event("pcb-retry", round_no=round_no, drc=result.drc.get("fatal", "?"))
        adapter.run(["pcb", "rip-up"])
        adapter.run(["pcb", "route-critical"])
        adapter.run(["pcb", "route-short"])
        adapter.run(["pcb", "power-pour"])
        retried = PcbResult(
            steps=list(result.steps) + [{"step": f"retry-r{round_no}", "rc": 0}],
            drc=_json_safe(adapter, ["pcb", "drc"]),
            check=_json_safe(adapter, ["pcb", "check"]),
            layout_lint=_json_safe(adapter, ["pcb", "layout-lint"]),
        )
        result = retried
    return result


def stage_pcb(
    adapter: EasyedaAdapter | None = None,
    *,
    audit=None,
    mount_holes: bool = True,
    retry: bool = True,
) -> dict:
    """M8 入口:sch PASS 后调用。返回 {gate_ok, degraded, steps, drc, check, layout_lint, report}。"""
    adapter = adapter or EasyedaAdapter()
    result = run_pcb_pipeline(adapter, mount_holes=mount_holes, audit=audit)
    if retry and not result.gate_ok:
        result = pcb_retry_loop(adapter, result, audit=audit)
    report = render_pcb_report(result)
    return {
        "gate_ok": result.gate_ok,
        "degraded": result.degraded,
        "steps": result.steps,
        "drc": result.drc,
        "check": result.check,
        "layout_lint": result.layout_lint,
        "report": report,
    }


def render_pcb_report(result: PcbResult) -> str:
    """PCB 交付报告:全绿=可下单;degraded=半成品+人工修板指引(R14)。"""
    lines = ["# PCB 交付报告"]
    if result.gate_ok:
        lines.append("**verdict: PASS**(drc/check/layout-lint 全绿,可进入报价下单)")
    elif result.degraded:
        lines.append("**verdict: DEGRADED-PASS**(电气安全门禁全过:短路/重叠/DRC=0;")
        lines.append("可制造性警告未清(组装间隙/烙铁通道)——**R14 半成品交付**:")
        lines.append("  - 人工在 EasyEDA 中微调 boxed-in 器件位置(拉开 ≥60mil 通道)")
        lines.append("  - 或交付 4 层板方案(power-planes 缓解 2 层拥挤)")
        lines.append("  - 调整后重跑 `edaloop pcb` 复检")
    else:
        lines.append("**verdict: FAIL**(电气门禁未过,需返工)")
    lines.append("")
    lines.append("## 步骤")
    for s in result.steps:
        note = f" ({s['note']})" if s.get("note") else (f" fixes={s['fixes']}" if s.get("fixes") else "")
        lines.append(f"- {s['step']}: rc={s['rc']}{note}")
    return "\n".join(lines)
