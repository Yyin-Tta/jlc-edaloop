from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from edaloop.generate.pcb import PIPELINE, PcbResult, pcb_retry_loop, run_pcb_pipeline
from edaloop.generate.ordering import order_draft, precheck_bom, quote


class _A:
    def __init__(self, rc_map: dict | None = None) -> None:
        self.rc_map = rc_map or {}
        self.calls: list[str] = []

    def run(self, args):
        self.calls.append(" ".join(args))
        return self.rc_map.get(args[1], 0), "{}", ""

    def run_json(self, args):
        return {
            "pcb": {"result": {"fatalCount": 0, "errorCount": 0, "verdict": "pass"}},
            "drc": {"result": {"fatalCount": 0}},
            "check": {"result": {"errorCount": 0}},
            "layout-lint": {"result": {"verdict": "pass"}},
        }[args[1]]


def test_pipeline_order_and_gates() -> None:
    a = _A()
    res = run_pcb_pipeline(a, mount_holes=False)
    names = [s["step"] for s in res.steps]
    assert names == [n for n, _ in PIPELINE]
    assert res.gate_ok is True


def test_pipeline_hard_stop_on_import_fail() -> None:
    a = _A(rc_map={"import-changes": 1})
    res = run_pcb_pipeline(a)
    names = [s["step"] for s in res.steps]
    assert "auto-place" not in names
    assert res.gate_ok is False


def test_pipeline_outline_fallback() -> None:
    a = _A(rc_map={"outline-fit": 1})
    res = run_pcb_pipeline(a, mount_holes=False)
    names = [s["step"] for s in res.steps]
    assert "outline-set-fallback" in names
    assert "pcb outline-set" in " ".join(a.calls)


def test_retry_loop_reroutes() -> None:
    a = _A()
    bad = PcbResult(drc={"fatal": 3}, check={"errors": 1}, layout_lint={"verdict": "fail"})
    out = pcb_retry_loop(a, bad, max_rounds=1)
    assert "pcb rip-up" in a.calls
    assert "pcb route-short" in a.calls
    assert out.gate_ok is True


def _bom(tmp_path: Path) -> str:
    p = tmp_path / "bom.json"
    p.write_text(
        json.dumps(
            {
                "total": 1.5,
                "details": [
                    {"ref": "C1", "qty": 2, "unit": 0.5, "line": 1.0},
                    {"ref": "NOPE", "qty": 1, "price": None},
                ],
            }
        ),
        encoding="utf-8",
    )
    return str(p)


def test_precheck_flags(tmp_path) -> None:
    from edaloop.generate.bomcost import PartCost

    with patch(
        "edaloop.generate.ordering.fetch_costs",
        side_effect=lambda ids: {"C1": PartCost("C1", price=0.5, stock=10, moq=5)},
    ):
        pre = precheck_bom(_bom(tmp_path))
    assert pre["ok"] is False
    issues = {p["issue"] for p in pre["problems"]}
    assert "no-lcsc" in issues
    assert "qty=1<MOQ=5" in issues or any("MOQ" in i for i in issues)


def test_quote_totals(tmp_path) -> None:
    q = quote(_bom(tmp_path), layers=2, qty=5)
    assert q.pcb_cost == 2.0
    assert q.parts_cost == 1.5
    assert q.total == q.pcb_cost + q.smt_cost + q.parts_cost


def test_order_draft_requires_confirm_path(tmp_path) -> None:
    from edaloop.generate.ordering import quote as _q

    q = _q(_bom(tmp_path))
    out = order_draft(q, "proj", out_dir=tmp_path / "od")
    text = out.read_text(encoding="utf-8")
    assert "未提交" in text and "支付" in text and "人工" in text
    assert "合计" in text
