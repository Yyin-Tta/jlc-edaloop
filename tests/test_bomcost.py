from __future__ import annotations

from unittest.mock import patch

from edaloop.generate.bomcost import PartCost, cost_hint_for_planner, fetch_cost, summarize_bom


def test_part_cost_dataclass() -> None:
    pc = PartCost(lcsc="C1", price=1.5, stock=10)
    assert pc.error == ""


def test_fetch_cost_invalid() -> None:
    pc = fetch_cost("")
    assert pc.error == "invalid lcsc"
    pc2 = fetch_cost("not-a-c-number")
    assert pc2.error == "invalid lcsc"


def test_fetch_cost_api_degrades(monkeypatch) -> None:
    import httpx

    class _BadResp:
        status_code = 200

        def json(self):
            return {"ok": False, "code": 500}

    def _fake_get(url, **kw):
        return _BadResp()

    monkeypatch.setattr(httpx, "get", _fake_get)
    pc = fetch_cost("C12345")
    assert pc.price is None and pc.error


def _fake_costs(lcscs):
    table = {
        "C1": PartCost("C1", price=2.0, stock=100),
        "C2": PartCost("C2", price=0.5, stock=3),
        "C3": PartCost("C3", price=None, error="api not ok"),
        "C4": PartCost("C4", price=1.0, stock=0),
    }
    return {c: table.get(c, PartCost(c, error="unknown")) for c in lcscs}


def test_summarize_bom_totals() -> None:
    with patch("edaloop.generate.bomcost.fetch_costs", _fake_costs):
        bom = summarize_bom(
            [
                {"instance": "a", "block_id": "b1", "lcsc": "C1"},
                {"instance": "a2", "block_id": "b1", "lcsc": "C1"},
                {"instance": "c", "block_id": "b2", "lcsc": "C2"},
                {"instance": "d", "block_id": "b3", "lcsc": "C3"},
                {"instance": "e", "block_id": "b4", "lcsc": ""},
                {"instance": "f", "block_id": "b5", "lcsc": "C4"},
            ]
        )
    assert bom["total"] == 2.0 * 2 + 0.5 * 1 + 1.0 * 1
    assert bom["priced_lines"] == 3
    assert any("C3" in n for n in bom["no_price"])
    assert any("no-lcsc" in n or "无 C 号" in n for n in bom["no_price"])
    assert any("C4" in n for n in bom["no_stock"])


def test_cost_hint_renders_groups() -> None:
    with patch("edaloop.generate.bomcost.fetch_costs", _fake_costs):
        hint = cost_hint_for_planner(
            {
                "power:ldo": [
                    {"block_id": "ldo-a", "lcsc": "C1"},
                    {"block_id": "ldo-b", "lcsc": "C2"},
                ]
            }
        )
    assert "ldo-a" in hint and "ldo-b" in hint
    assert "¥2" in hint and "¥0.5" in hint


def test_cost_hint_skips_single() -> None:
    assert cost_hint_for_planner({"x": [{"block_id": "only", "lcsc": "C1"}]}) == ""
