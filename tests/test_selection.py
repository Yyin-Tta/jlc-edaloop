from __future__ import annotations

from unittest.mock import patch

from edaloop.generate.selection import (
    SwapProposal,
    annotate_smt,
    proposals_report,
    propose_swaps,
    smt_library_type,
)


def test_smt_type_heuristic(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(Exception("net down")))
    assert smt_library_type("C6186") == "basic"
    assert smt_library_type("C9900013921") == "extended"
    assert smt_library_type("") == "unknown"
    assert smt_library_type("XX") == "unknown"


class _R:
    def __init__(self, payload: dict) -> None:
        self._p = payload

    def json(self):
        return self._p


def test_smt_type_from_api(monkeypatch) -> None:
    import httpx

    def fake_get(url, **kw):
        return _R({"ok": True, "result": {"componentLibraryType": "Base"}})

    monkeypatch.setattr(httpx, "get", fake_get)
    assert smt_library_type("C8734") == "base"


def test_annotate_smt_batch() -> None:
    with patch("edaloop.generate.selection.smt_library_type", side_effect=lambda c: "basic"):
        out = annotate_smt(["C1", "C2"])
        assert out == {"C1": "basic", "C2": "basic"}


def _cost(c, p):
    from edaloop.generate.bomcost import PartCost

    return PartCost(lcsc=c, price=p, stock=100)


def test_propose_swaps_picks_cheapest() -> None:
    table = {"C1": _cost("C1", 2.0), "C2": _cost("C2", 0.8), "C3": _cost("C3", 5.0)}
    with patch("edaloop.generate.selection.fetch_costs", side_effect=lambda ids: {c: table[c] for c in ids}):
        props = propose_swaps(
            {"power:ldo": [{"block_id": "ldo-a", "lcsc": "C3"}, {"block_id": "ldo-b", "lcsc": "C1"}, {"block_id": "ldo-c", "lcsc": "C2"}]}
        )
    assert len(props) == 2
    assert all(p.to_block == "ldo-c" for p in props)
    top = max(props, key=lambda p: p.saving_pct)
    assert top.from_block == "ldo-a" and abs(top.saving_pct - 84.0) < 0.1


def test_propose_swaps_skip_small_gain() -> None:
    table = {"C1": _cost("C1", 1.0), "C2": _cost("C2", 0.98)}
    with patch("edaloop.generate.selection.fetch_costs", side_effect=lambda ids: {c: table[c] for c in ids}):
        props = propose_swaps({"x": [{"block_id": "a", "lcsc": "C1"}, {"block_id": "b", "lcsc": "C2"}]})
    assert props == []


def test_report_renders() -> None:
    p = SwapProposal("fn", "blk-a", "blk-b", "C3", "C2", _cost("C3", 5.0), _cost("C2", 0.8), 84.0)
    rep = proposals_report([p])
    assert "blk-a" in rep and "84.0%" in rep and "弱门禁" in rep
    assert "无 swap 提案" in proposals_report([])
