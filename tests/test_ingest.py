from __future__ import annotations

import pytest

from edaloop.ingest.extract import rule_channel
from edaloop.ingest.models import PinInfo, PinTable
from edaloop.ingest.pdf_pages import (
    elec_rows,
    find_elec_pages,
    find_pin_pages,
    page_text,
    rule_extract,
)
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


def test_run_gate_empty_rule_degrades() -> None:
    llm = PinTable(
        part="X",
        source_pdf="x.pdf",
        pages=[1],
        pins=[PinInfo(number=str(i), name=f"P{i}", page=1, channel="llm") for i in range(1, 9)],
    )
    assert run_gate(llm, []).verdict == "low-confidence"


def test_rule_extract_dup_number_rejected() -> None:
    """MAX485 页7 实测:同名脚两次且名字不同(1→{B,D}、7→{Rt,A})= 相邻行解析错位 → 整页放弃。"""
    text = "Rt\n7\nB\n1\nD\n1\nDIP/SO\n2\nA\n7\n"
    assert rule_extract(text, 7) == []


def test_rule_extract_same_name_dup_preserved() -> None:
    """ULN2003A DIP/SOIC 双列:同名脚冗余(1→{1B,1B})映射仍唯一可靠 → 保留。"""
    text = "1B\n1\n2B\n2\n1B\n1\n2B\n2\nCOM\n9\n"
    pins = rule_extract(text, 3)
    assert {p["number"]: p["name"] for p in pins}["1"] == "1B"
    assert len(pins) == 5


def test_rule_extract_io_type_noise_filtered() -> None:
    """esp32-s3 页22:"TYPE"/"I1/O/T"/"I0/O/T" 是表头与 io_type 词元,非引脚名 → 规则通道空。"""
    text = "TYPE\n35\nI1/O/T\n39\nI0/O/T\n40\n"
    assert rule_extract(text, 22) == []


def test_rule_extract_column_value_noise_filtered() -> None:
    """esp32-s3 页79 合并式总览:「Analog/Power」列值紧邻下一个脚号被误对(同名→多脚)→ 整页放弃。

    真脚名(GND/VCC)至多映射 4 脚,列值映射半个表(28/20 脚),阈值 _NAME_MAX_PINS 切开。
    """
    text = "".join(f"{n}\nX{n}\nAnalog\n" for n in range(1, 9))  # Analog→8 个脚号
    assert rule_extract(text, 79) == []


def test_run_gate_reserved_pin_small_gap() -> None:
    """esp32-s3 缺 46(1 脚保留/NC):空 rule + 小缺口(1 脚)→ low-confidence,不误杀。"""
    llm = PinTable(
        part="X",
        source_pdf="x.pdf",
        pages=[1],
        pins=[PinInfo(number=str(i), name=f"P{i}", page=1, channel="llm") for i in list(range(35, 46)) + list(range(47, 52))],
    )
    rep = run_gate(llm, [])
    assert rep.verdict == "low-confidence"
    assert any("缺 [46]" in v for v in rep.internal_violations)  # 跳号仍记账


def test_run_gate_large_gap_fail() -> None:
    """CH340C 缺 15-18(4 脚漏提):空 rule + 大缺口(≥3 脚)→ fail,保持 fail-closed。"""
    llm = PinTable(
        part="X",
        source_pdf="x.pdf",
        pages=[1],
        pins=[PinInfo(number=str(i), name=f"P{i}", page=1, channel="llm") for i in list(range(1, 15)) + [19]],
    )
    assert run_gate(llm, []).verdict == "fail"


# ---- P4-6②/G16:电气参数表页定位 + min/typ/max 机械提取 ----


def test_find_elec_pages() -> None:
    pages = find_elec_pages("evals/datasheets/ULN2003A_ti.pdf")
    assert 2 in pages and 5 in pages  # abs-max 概览页 + Electrical Characteristics 正文页


def test_elec_rows_real_ti_pdf() -> None:
    """真件回归:TI 纵向列流版式,视觉行聚类必须出非零行且含命名正确的参数行。"""
    rows = elec_rows("evals/datasheets/ULN2003A_ti.pdf", 5)
    assert len(rows) >= 5
    sat = next(r for r in rows if "VCE(sat)" in r["param"])
    assert sat["min"] == "1" and sat["max"] == "1.3" and sat["unit"] == "V"  # 页5首行 VCE(sat)@350uA


def test_elec_rows_column_flow_pdf(tmp_path) -> None:
    """合成列流 PDF:同一视觉行的单元格(不同 x)必须聚成一行,不依赖 text 流顺序。"""
    import pymupdf

    pdf = tmp_path / "col.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        # 纵向列流:参数名/测试条件/min/max/单位 各自独立 text 插入,y 微差在聚类容差内
        page.insert_text((72, 100), "Supply voltage VCC")
        page.insert_text((260, 101), "VI = 5 V")
        page.insert_text((360, 100), "4.5")
        page.insert_text((410, 101), "5.5")
        page.insert_text((460, 100), "V")
        doc.save(pdf)
    rows = elec_rows(str(pdf), 1)
    assert len(rows) == 1
    r = rows[0]
    assert "Supply voltage" in r["param"]
    assert r["min"] == "4.5" and r["max"] == "5.5" and r["unit"] == "V"


def test_elec_rows_prose_single_value_rejected(tmp_path) -> None:
    """prose 单数值+单位行(典型值句式)不进表——≥2 数值从严阈值。"""
    import pymupdf

    pdf = tmp_path / "prose.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 100), "The typical supply current is 3.5 mA per channel under normal operation.")
        doc.save(pdf)
    assert elec_rows(str(pdf), 1) == []


def test_ingest_pdf_accepts_3pin_part(tmp_path, monkeypatch) -> None:
    """P5-1 回归:3 脚器件(SOT-223 LDO)必须能走通全管道并单通道降级入库。

    旧 ≥4 采纳阈值把 3 脚件逼向「合并多封装凑 12 脚」(AMS1117 run 2026-09-01),
    内部一致性门禁判 fail 后整批 0 入库。
    """
    import json

    import pymupdf

    from edaloop.ingest.pipeline import ingest_pdf
    from edaloop.ingest.store import DatasheetStore
    from edaloop.llm.fake import FakeChat

    monkeypatch.chdir(tmp_path)  # AuditLog("runs/ingest") 落 tmp,不污染生产审计流
    body = (
        "PIN CONNECTIONS\nAMS1117 1A LOW DROPOUT REGULATOR SOT-223 3 PIN\n"
        + "filler " * 40 + "\n1- Ground/Adjust\n2- VOUT\n3- VIN\n"
    )
    pdf = tmp_path / "ams.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 100), body)
        doc.save(pdf)
    reply = json.dumps(
        {
            "part": "AMS1117",
            "pins": [
                {"number": "1", "name": "Ground/Adjust", "io_type": "I"},
                {"number": "2", "name": "VOUT", "io_type": "O"},
                {"number": "3", "name": "VIN", "io_type": "I"},
            ],
        }
    )
    db = str(tmp_path / "kb.db")
    table, report = ingest_pdf(str(pdf), FakeChat(reply), db_path=db)
    assert report.verdict == "low-confidence"
    assert len(table.pins) == 3
    store = DatasheetStore(db)
    got = store.get("AMS1117")
    assert got is not None
    assert {p.name for p in got.pins} == {"Ground/Adjust", "VOUT", "VIN"}


def test_ingest_pdf_picks_most_complete_table(tmp_path, monkeypatch) -> None:
    """esp32-s3 回归:候选页里有多张 pin 表(部分总览 30 脚 vs 合并式完整总览 56 脚)。

    聚合必须取脚数最多的完整表,而非先出现的部分表——旧 break-on-overlap 会在第一张
    (Table 2-1 部分总览)重叠处停住,漏掉后文 56 脚的合并式完整总览(Table 7-1)。
    """
    import json

    import pymupdf

    from edaloop.ingest.pipeline import ingest_pdf
    from edaloop.llm.fake import FakeChat

    monkeypatch.chdir(tmp_path)
    pdf = tmp_path / "soc.pdf"
    with pymupdf.open() as doc:
        p1 = doc.new_page()
        p1.insert_text(
            (72, 100),
            "Pin Configuration\n" + "filler " * 30 + "\n" + "\n".join(str(i) for i in range(1, 31)),
        )
        p2 = doc.new_page()
        p2.insert_text(
            (72, 100),
            "Consolidated Pin Overview\n" + "filler " * 30 + "\n" + "\n".join(str(i) for i in range(1, 57)),
        )
        doc.save(pdf)

    def pin_json(n: int) -> str:
        return json.dumps(
            {
                "part": "SOC",
                "pins": [{"number": str(i), "name": f"GPIO{i}", "io_type": "I/O"} for i in range(1, n + 1)],
            }
        )

    # 第 1 次 chat = 页 1(30 脚);第 2 次 = 页 2(56 脚);后续 suggestions 调用耗尽后重复末条(不崩溃)。
    table, report = ingest_pdf(str(pdf), FakeChat([pin_json(30), pin_json(56)]), db_path=str(tmp_path / "kb.db"))
    assert len(table.pins) == 56  # 取完整总览,非先出现的部分表
    assert report.verdict in ("pass", "low-confidence")


def test_ingest_pdf_tie_prefers_rule_supported_pages(tmp_path, monkeypatch) -> None:
    """wroom-1 回归:图页与表页并列(同脚数)时取规则通道可解析的表页 run。

    Figure 3-1 引脚布局图(散排标签)LLM 能重建全脚位,但规则通道在图页只产出错位对;
    旧「先出现者」并列判据把证据让给图页 → 双通道交叉校验结构性失效(实测 41 vs 41 取图页,
    rule 在图页出 7 条错位对 → 全 disagree 判 fail)。
    """
    import json

    import pymupdf

    from edaloop.ingest.pipeline import ingest_pdf
    from edaloop.llm.fake import FakeChat

    monkeypatch.chdir(tmp_path)
    pdf = tmp_path / "wroom.pdf"
    with pymupdf.open() as doc:
        p1 = doc.new_page()  # 图页:Pin Layout 散排标签,同名同号但非表格邻接
        p1.insert_text(
            (72, 100),
            "Pin Definitions\n3.1 Pin Layout (Top View)\n" + "filler " * 30 + "\nGND 3V3 EN IO0\n1 2 3 4",
        )
        p2 = doc.new_page()  # 表页:Name/No 各占一行(PDF 文本流逐 token 分行),规则通道可解析
        p2.insert_text(
            (72, 100),
            "Pin Definitions\nTable 3-1. Pin Definitions\n" + "filler " * 30 + "\nGND\n1\n3V3\n2\nEN\n3\nIO0\n4",
        )
        doc.save(pdf)

    def pin_json() -> str:
        return json.dumps(
            {
                "part": "WROOM",
                "pins": [
                    {"number": "1", "name": "GND", "io_type": "P"},
                    {"number": "2", "name": "3V3", "io_type": "P"},
                    {"number": "3", "name": "EN", "io_type": "I"},
                    {"number": "4", "name": "IO0", "io_type": "I/O"},
                ],
            }
        )

    # 两页 LLM 各返回同构 4 脚(并列);规则通道只在表页出 4 对,图页散排标签 0 对。
    table, report = ingest_pdf(str(pdf), FakeChat([pin_json(), pin_json()]), db_path=str(tmp_path / "kb.db"))
    assert table.pages == [2]  # 取表页 run,非先出现的图页
    assert report.rule_pins >= 3  # 双通道交叉校验活着
    assert report.verdict in ("pass", "low-confidence")


def test_llm_extract_normalizes_io_type_slash_t() -> None:
    """Espressif 风格 io_type "I/O/T"(T=触摸能力)归一为 "I/O",不让门禁误判非法。"""
    import json

    from edaloop.ingest.extract import llm_extract
    from edaloop.llm.fake import FakeChat

    reply = json.dumps(
        {
            "part": "SOC",
            "pins": [
                {"number": "1", "name": "IO0", "io_type": "I/O/T"},
                {"number": "2", "name": "GND", "io_type": "P"},
                {"number": "3", "name": "TXD0", "io_type": "O/T"},
                {"number": "4", "name": "RXD0", "io_type": "I/T"},
            ],
        }
    )
    t = llm_extract("PIN DESCRIPTION\n" + "filler " * 40, "soc.pdf", FakeChat(reply), 1)
    assert [p.io_type for p in t.pins] == ["I/O", "P", "O", "I"]


def test_ingest_pdf_non_digit_continuation_page_rejected(tmp_path, monkeypatch) -> None:
    """wroom-1 页 13 回归:无重叠 ≠ 续表。strapping 脚表把 GPIO 名当号(GPIO0/GPIO3),

    非数字号不得并入主表(旧逻辑 41+4=45 超集);它自成 run,脚数小自然落选。
    """
    import json

    import pymupdf

    from edaloop.ingest.pipeline import ingest_pdf
    from edaloop.llm.fake import FakeChat

    monkeypatch.chdir(tmp_path)
    pdf = tmp_path / "wroom2.pdf"
    with pymupdf.open() as doc:
        p1 = doc.new_page()
        p1.insert_text(
            (72, 100),
            "Pin Definitions\nTable 3-1. Pin Definitions\n" + "filler " * 30 + "\nGND\n1\n3V3\n2\nEN\n3\nIO0\n4",
        )
        p2 = doc.new_page()
        p2.insert_text(
            (72, 100),
            "Table 3-1 - cont'd from previous page\n" + "filler " * 30 + "\nIO1\n5\nIO2\n6\nIO3\n7\nIO4\n8",
        )
        p3 = doc.new_page()
        p3.insert_text(
            (72, 100),
            "Boot Configurations strapping pins\n" + "filler " * 30 + "\nGPIO0 strapping\nGPIO3 strapping\nGPIO45",
        )
        doc.save(pdf)

    def pins(items: list[tuple[str, str, str]]) -> str:
        return json.dumps(
            {"part": "WROOM", "pins": [{"number": n, "name": nm, "io_type": io} for n, nm, io in items]}
        )

    main4 = pins([("1", "GND", "P"), ("2", "3V3", "P"), ("3", "EN", "I"), ("4", "IO0", "I/O")])
    cont4 = pins([("5", "IO1", "I/O"), ("6", "IO2", "I/O"), ("7", "IO3", "I/O"), ("8", "IO4", "I/O")])
    strap = pins(
        [("GPIO0", "GPIO0", "S"), ("GPIO3", "GPIO3", "S"), ("GPIO45", "GPIO45", "S"), ("GPIO46", "GPIO46", "S")]
    )

    table, report = ingest_pdf(str(pdf), FakeChat([main4, cont4, strap]), db_path=str(tmp_path / "kb.db"))
    assert table.pages == [1, 2]  # 数字续页并入,strapping 页自成 run 落选
    assert len(table.pins) == 8
    assert all(p.number.isdigit() for p in table.pins)  # GPIO0 类号不入表
