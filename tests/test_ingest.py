from __future__ import annotations

import pytest

from edaloop.ingest.extract import rule_channel
from edaloop.ingest.models import PinInfo, PinTable
from edaloop.ingest.pdf_pages import find_pin_pages, page_text, rule_extract
from edaloop.ingest.validate import check_internal, compare_channels, run_gate

_ULN_TEXT = """Table 4-1. Pin Functions
PIN
I/O
DESCRIPTION
NAME
NO.
1B
1
I
Channel 1 through 7 Darlington base input
2B
2
3B
3
4B
4
5B
5
6B
6
7B
7
1C
16
O
Channel 1 through 7 Darlington collector output
2C
15
3C
14
4C
13
5C
12
6C
11
7C
10
COM
9
P
Common cathode node for flyback diodes
E
8
P
Common emitter shared by all channels
"""


def test_find_pin_pages() -> None:
    pages = find_pin_pages("evals/datasheets/ULN2003A_ti.pdf")
    assert 3 in pages


def test_rule_extract_uln() -> None:
    pins = rule_extract(_ULN_TEXT, 3)
    by_no = {p["number"]: p for p in pins}
    assert by_no["1"]["name"] == "1B"
    assert by_no["16"]["name"] == "1C"
    assert by_no["9"]["name"] == "COM"
    assert by_no["8"]["name"] == "E"
    assert len(pins) == 16


def test_rule_channel_bare_fallback() -> None:
    pins = rule_channel("1B\n1\n2B\n2\nCOM\n9\nE\n8\n", 3)
    assert {p.number for p in pins} == {"1", "2", "9", "8"}


def test_internal_consistency() -> None:
    good = PinTable(
        part="X",
        source_pdf="x.pdf",
        pages=[1],
        pins=[PinInfo(number=str(i), name=f"P{i}", io_type="I", page=1, channel="llm") for i in (1, 2, 3)],
    )
    assert check_internal(good) == []
    dup = good.model_copy(deep=True)
    dup.pins.append(PinInfo(number="2", name="XX", io_type="I", page=1, channel="llm"))
    v = check_internal(dup)
    assert any("重复" in x for x in v)
    gap = PinTable(
        part="X",
        source_pdf="x.pdf",
        pages=[1],
        pins=[PinInfo(number="1", name="A", page=1, channel="llm"), PinInfo(number="3", name="B", page=1, channel="llm")],
    )
    assert any("不连续" in x for x in check_internal(gap))


def test_compare_channels() -> None:
    llm = PinTable(
        part="X",
        source_pdf="x.pdf",
        pages=[1],
        pins=[
            PinInfo(number="1", name="1B", page=1, channel="llm"),
            PinInfo(number="2", name="2B", page=1, channel="llm"),
        ],
    )
    rule = [
        PinInfo(number="1", name="1B", page=1, channel="rule"),
        PinInfo(number="2", name="XX", page=1, channel="rule"),
    ]
    dis = compare_channels(llm, rule)
    assert len(dis) == 1 and "2" in dis[0]
    assert llm.pins[1].agreed is False
    assert llm.pins[0].agreed is True


def test_run_gate_verdicts() -> None:
    llm = PinTable(
        part="X",
        source_pdf="x.pdf",
        pages=[1],
        pins=[PinInfo(number=str(i), name=f"P{i}", page=1, channel="llm") for i in range(1, 9)],
    )
    rule = [PinInfo(number=str(i), name=f"P{i}", page=1, channel="rule") for i in range(1, 9)]
    assert run_gate(llm, rule).verdict == "pass"
    rule[0] = PinInfo(number="1", name="WRONG", page=1, channel="rule")
    assert run_gate(llm, rule).verdict == "low-confidence"
