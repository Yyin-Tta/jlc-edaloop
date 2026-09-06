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


def test_fetch_cost_retries_transient_once(monkeypatch) -> None:
    """P5-4②:SSL/连接瞬态重试一次,首败次成 → 有价。"""
    import httpx

    calls = {"n": 0}

    class _OkResp:
        status_code = 200

        def json(self):
            return {
                "ok": True,
                "result": {
                    "productPriceList": [{"ladder": 1, "currencyPrice": "0.42"}],
                    "stockNumber": "500",
                },
            }

    def _fake_get(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return _OkResp()

    monkeypatch.setattr(httpx, "get", _fake_get)
    pc = fetch_cost("C12345")
    assert pc.price == 0.42
    assert calls["n"] == 2


def test_fetch_cost_transient_twice_degrades(monkeypatch) -> None:
    """两次都瞬态失败 → 落 error 不抛(弱信号不抛原则)。"""
    import httpx

    def _fake_get(url, **kw):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "get", _fake_get)
    pc = fetch_cost("C12345")
    assert pc.price is None
    assert "ConnectError" in pc.error


def test_summarize_bom_exempt_c99xx() -> None:
    """C99xx 无价 → 豁免(不进有价分母);非 C99xx 有价照常进。"""
    def _fake(lcscs):
        return {
            c: (PartCost(c, price=None, error="no business data") if c.startswith("C99") else PartCost(c, price=1.0))
            for c in lcscs
        }

    with patch("edaloop.generate.bomcost.fetch_costs", _fake):
        bom = summarize_bom(
            [
                {"instance": "a", "block_id": "b1", "lcsc": "C9912345"},
                {"instance": "b", "block_id": "b2", "lcsc": "C1000"},
            ]
        )
    cov = bom["coverage"]
    assert cov["exempt_c99xx"] == 1
    assert cov["exempt_lines"] == 1
    assert cov["non_exempt_lines"] == 1
    assert cov["coverage"] == 1.0


def test_summarize_bom_exempt_std_value() -> None:
    """std 无值件(resistor/capacitor-std)无 C 号 → 豁免;up-* 无 lcsc 是真缺。"""
    with patch("edaloop.generate.bomcost.fetch_costs", lambda lcscs: {}):
        bom = summarize_bom(
            [
                {"instance": "r1", "block_id": "resistor-std", "lcsc": ""},
                {"instance": "c1", "block_id": "capacitor-std", "lcsc": ""},
                {"instance": "x", "block_id": "up-esp32_autodownload", "lcsc": ""},
            ]
        )
    cov = bom["coverage"]
    assert cov["exempt_std"] == 2
    assert cov["exempt_lines"] == 2
    assert cov["non_exempt_lines"] == 1
    assert cov["coverage"] == 0.0


def test_summarize_bom_coverage_denominator() -> None:
    """覆盖率分母 = 总行 − 豁免行;真缺 no-price 仍进分母。"""
    def _fake(lcscs):
        out = {}
        for c in lcscs:
            if c.startswith("C99"):
                out[c] = PartCost(c, price=None, error="no business data")
            elif c == "C500":
                out[c] = PartCost(c, price=None, error="api not ok")
            else:
                out[c] = PartCost(c, price=1.0)
        return out

    with patch("edaloop.generate.bomcost.fetch_costs", _fake):
        bom = summarize_bom(
            [
                {"instance": "a", "block_id": "b1", "lcsc": "C1"},
                {"instance": "b", "block_id": "b2", "lcsc": "C2"},
                {"instance": "c", "block_id": "b3", "lcsc": "C991"},
                {"instance": "d", "block_id": "b4", "lcsc": "C500"},
            ]
        )
    cov = bom["coverage"]
    assert cov["total_lines"] == 4
    assert cov["exempt_lines"] == 1
    assert cov["non_exempt_lines"] == 3
    assert cov["priced_lines"] == 2
    assert cov["coverage"] == 0.6667
