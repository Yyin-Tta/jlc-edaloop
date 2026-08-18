from __future__ import annotations

import re

import pymupdf

_PIN_PAGE_MARKERS = (
    "pin configuration and functions",
    "pin description",
    "pin functions",
    "pin definition",
    "pin assignments",
    "terminal functions",
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
