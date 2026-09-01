from __future__ import annotations

import re

import pymupdf

_PIN_PAGE_MARKERS = (
    "pin configuration and functions",
    "pin description",
    "pin functions",
    "pin definition",
    "pin assignments",
    "pin connection",
    "terminal functions",
    "pin configuration",
    "internal connection",  # Sharp/Renesas 光耦等:「Internal Connection Diagram」即引脚定义页(PC817 首跑漏页)
)

# P4-6②/G16:电气参数表页定位标记(数值表通道)。
_ELEC_PAGE_MARKERS = (
    "electrical characteristics",
    "recommended operating conditions",
    "absolute maximum ratings",
    "absolute maximum",
    "operating conditions",
)


def find_pin_pages(pdf_path: str) -> list[int]:
    """返回 1-based 页码列表:含 pin 表证据页。"""
    hits: list[int] = []
    with pymupdf.open(pdf_path) as doc:
        for i, page in enumerate(doc):
            low = page.get_text().lower()
            if any(m in low for m in _PIN_PAGE_MARKERS):
                hits.append(i + 1)
    return hits


def find_elec_pages(pdf_path: str) -> list[int]:
    """返回 1-based 页码列表:含电气参数表证据页(P4-6②)。"""
    hits: list[int] = []
    with pymupdf.open(pdf_path) as doc:
        for i, page in enumerate(doc):
            low = page.get_text().lower()
            if any(m in low for m in _ELEC_PAGE_MARKERS):
                hits.append(i + 1)
    return hits


def page_count(pdf_path: str) -> int:
    with pymupdf.open(pdf_path) as doc:
        return len(doc)


def page_text(pdf_path: str, page_no: int) -> str:
    with pymupdf.open(pdf_path) as doc:
        return doc[page_no - 1].get_text()


_NAME_RE = re.compile(r"^([0-9A-Za-z][0-9A-Za-z_/\-+]*)$")
_NO_RE = re.compile(r"^(\d{1,3})$")


def _is_name(token: str) -> bool:
    if token in _NAME_NOISE or len(token) > 6:
        return False
    return any(c.isalpha() for c in token)

_NAME_NOISE = {
    "PIN", "NO", "NAME", "DESCRIPTION", "I", "O", "UNIT", "MIN", "MAX", "TYPE",
    "Figure", "Table", "Copyright", "www", "com", "Product", "Folder", "Links",
    "Submit", "Document", "Feedback", "SLRS027T", "DECEMBER", "REVISED", "MARCH",
}


def _adjacent_pairs(text: str) -> list[tuple[str, str]]:
    """PDF 文本流是逐 token 分行('1B' 行紧跟 '1' 行)。返回 (name, no) 相邻对。"""
    pairs: list[tuple[str, str]] = []
    pending: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            pending = None
            continue
        m = _NAME_RE.match(line)
        if m and _is_name(line):
            pending = line
            continue
        m = _NO_RE.match(line)
        if m and pending:
            pairs.append((pending, m.group(1)))
            pending = None
            continue
        pending = None
    return pairs


def rule_extract(text: str, page_no: int) -> list[dict]:
    """规则通道:相邻行对提取 (name, no)。比对通道只需 number→name。"""
    return [
        {
            "number": no,
            "name": name,
            "io_type": "",
            "desc": "",
            "page": page_no,
            "channel": "rule",
        }
        for name, no in _adjacent_pairs(text)
    ]


def rule_extract_bare(text: str, page_no: int) -> list[dict]:
    return rule_extract(text, page_no)


# ---- P4-6②/G16:电气参数表 min/typ/max 数值行提取(机械通道) ----

_NUM_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_UNITS = {
    "v": "V", "volt": "V", "volts": "V", "vdc": "V",
    "mv": "mV", "uv": "uV",
    "a": "A", "ma": "mA", "ua": "uA",
    "w": "W", "mw": "mW",
    "hz": "Hz", "khz": "kHz", "mhz": "MHz",
    "c": "°C", "°c": "°C", "db": "dB", "ω": "Ω", "ohm": "Ω",
}
_ELEC_ROW_NOISE = ("copyright", "www", "datasheet", "revision")


def _norm_unit(tok: str) -> str:
    t = tok.strip().rstrip(".,;:").lower().replace("µ", "u").replace("μ", "u")
    return _UNITS.get(t, "")


def _visual_lines(words: list[tuple]) -> list[list[str]]:
    """words(y 坐标 ±2.5pt 聚类)→ 视觉行 token 序(x 升序)。

    datasheet 表格在 get_text() 文本流里是纵向列流(一行的单元格散成多条 text 行),
    按 y 聚类才能还原视觉行——TI 版式实测文本流行提取零命中。
    """
    lines: list[tuple[float, list[tuple]]] = []
    for w in sorted(words, key=lambda w: (w[1], w[0])):
        for yk, bucket in lines:
            if abs(yk - w[1]) <= 2.5:
                bucket.append(w)
                break
        else:
            lines.append((w[1], [w]))
    return [[w[4] for w in sorted(bucket, key=lambda w: w[0])] for _, bucket in lines]


def elec_rows(pdf_path: str, page_no: int) -> list[dict]:
    """机械提取 min/typ/max 数值行:视觉行尾连续 ≥2 个纯数字(+可选单位)→ 前缀为参数名。

    只在 find_elec_pages 命中页上跑;≥2 数值从严防 prose 误报——
    单数值+单位行大量出现在正文,不可机械信任。
    """
    with pymupdf.open(pdf_path) as doc:
        words = doc[page_no - 1].get_text("words")
    rows: list[dict] = []
    for toks in _visual_lines(words):
        line = " ".join(toks)
        if len(line) > 160:
            continue
        low = line.lower()
        if any(n in low for n in _ELEC_ROW_NOISE):
            continue
        if len(toks) < 3:
            continue
        # 行尾数字 run(2-4 个),其后允许一个单位 token
        i = len(toks)
        unit = ""
        if toks and _norm_unit(toks[-1]):
            unit = _norm_unit(toks[-1])
            i -= 1
        nums: list[str] = []
        while i > 0 and _NUM_RE.match(toks[i - 1]) and len(nums) < 4:
            nums.insert(0, toks[i - 1])
            i -= 1
        if len(nums) < 2:
            continue
        name = " ".join(toks[:i]).rstrip(":-–—").strip()
        if len(name) < 3 or not any(c.isalpha() for c in name):
            continue
        # 3 数 = min/typ/max;2 数按 datasheet 惯例 = min/max(ROC/abs-max 行缺 typ 列)
        if len(nums) >= 3:
            min_v, typ_v, max_v = nums[0], nums[1], nums[2]
        else:
            min_v, typ_v, max_v = nums[0], "", nums[1]
        rows.append(
            {
                "param": name[:80],
                "min": min_v,
                "typ": typ_v,
                "max": max_v,
                "unit": unit,
                "page": page_no,
                "channel": "rule",
            }
        )
    return rows
