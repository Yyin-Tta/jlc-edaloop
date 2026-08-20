from __future__ import annotations

from edaloop.generate.freeform import decompose, match_pattern
from edaloop.knowledge.models import RetrievedBlock


def _cand(block_id: str, lcsc: str) -> RetrievedBlock:
    return RetrievedBlock(
        block_id=block_id,
        name=block_id,
        desc="x",
        category="power",
        tags=[],
        parts=[],
        ports=[],
        provenance="",
        lcsc=lcsc,
        pinout={"1": "A", "2": "B"},
        score=1.0,
        channels=["dense"],
    )


def test_match_pattern_liion() -> None:
    pat = match_pattern("单节锂电池 过充 过放 过流保护 板")
    assert pat is not None and pat["id"] == "liion-protection"


def test_match_pattern_can() -> None:
    pat = match_pattern("挂一路 CAN 2.0 总线收发")
    assert pat is not None and pat["id"] == "can-node"


def test_match_pattern_none() -> None:
    assert match_pattern("USB 转串口下载") is None


def test_decompose_liion_wiring() -> None:
    pat = match_pattern("锂电保护")
    assert pat is not None
    cands = {
        "battery-dw01-protection": _cand("battery-dw01-protection", "C2927799"),
        "mos-fs8205a-dual": _cand("mos-fs8205a-dual", "C2830320"),
    }
    blocks, notes = decompose(pat, cands, "liion")
    assert len(blocks) == 2
    dw01 = next(b for b in blocks if b.instance == "liion_dw01")
    fs = next(b for b in blocks if b.instance == "liion_fs")
    # DW01 的 OD/OC 与 FS 的 G1/G2(6/4) 交叉互联:同网名即连通
    assert dw01.pins_binding["1"] == fs.pins_binding["6"]
    assert dw01.pins_binding["3"] == fs.pins_binding["4"]
    # 电源/地已绑定
    assert dw01.pins_binding["6"] == "GND"
    assert fs.pins_binding["1"] == "GND"
    assert notes


def test_decompose_missing_part() -> None:
    pat = match_pattern("锂电保护")
    assert pat is not None
    blocks, notes = decompose(pat, {}, "liion")
    assert blocks == []
    assert any("缺原料" in n for n in notes)


def test_decompose_can() -> None:
    pat = match_pattern("CAN 总线")
    assert pat is not None
    cands = {"can-tja1051": _cand("can-tja1051", "C9900013921")}
    blocks, _ = decompose(pat, cands, "can")
    assert len(blocks) == 1
    b = blocks[0]
    assert b.pins_binding["3"] == "5V"
    assert b.pins_binding["2"] == "GND"
    assert "CANH" in b.pins_binding["8"]
