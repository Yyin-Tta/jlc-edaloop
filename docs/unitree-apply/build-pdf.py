# -*- coding: utf-8 -*-
"""把 02-申请书.md / 03-开发总纲摘录.md 转成 PDF(md→HTML→Edge 无头打印)。

用法:uv run --with markdown python docs/unitree-apply/build-pdf.py
依赖:本仓库 venv + 临时安装 markdown;系统 Edge(渲染 CJK/表格/图片)。
"""
import pathlib
import subprocess

import markdown

BASE = pathlib.Path(__file__).parent
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

CSS = """
@page { size: A4; margin: 18mm 16mm; }
body { font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
       font-size: 10.5pt; line-height: 1.65; color: #1a1a1a; margin: 0; }
h1 { font-size: 17pt; border-bottom: 2px solid #333; padding-bottom: 6px; margin: 0 0 14px; }
h2 { font-size: 13pt; margin: 20px 0 8px; border-left: 4px solid #2b6cb0; padding-left: 8px; }
h3 { font-size: 11.5pt; margin: 14px 0 6px; }
p { margin: 6px 0; text-align: justify; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 9.5pt; page-break-inside: avoid; }
th, td { border: 1px solid #999; padding: 4px 7px; text-align: left; vertical-align: top; }
th { background: #eef2f7; }
code { font-family: Consolas, monospace; background: #f2f2f2; padding: 1px 4px;
       border-radius: 3px; font-size: 9pt; }
pre { background: #f6f8fa; border: 1px solid #ddd; border-radius: 4px;
      padding: 8px 10px; overflow-x: auto; page-break-inside: avoid; }
pre code { background: none; padding: 0; }
blockquote { border-left: 4px solid #b0b7c0; margin: 8px 0; padding: 2px 12px;
             color: #444; background: #f7f8fa; }
img { max-width: 100%; page-break-inside: avoid; margin: 8px 0; }
a { color: #2b6cb0; text-decoration: none; word-break: break-all; }
hr { border: none; border-top: 1px solid #ccc; margin: 14px 0; }
ul, ol { margin: 6px 0; padding-left: 22px; }
li { margin: 3px 0; }
"""

JOBS = [
    ("02-申请书.md", "jlc-edaloop-申请书-郭英涛.pdf"),
    ("03-开发总纲摘录.md", "jlc-edaloop-开发总纲摘录-郭英涛.pdf"),
]


def convert(md_name: str, pdf_name: str) -> None:
    body = markdown.markdown(
        (BASE / md_name).read_text(encoding="utf-8"),
        extensions=["tables", "fenced_code", "sane_lists"],
    )
    html_path = BASE / (md_name + ".tmp.html")
    html_path.write_text(
        f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head>"
        f"<body>{body}</body></html>",
        encoding="utf-8",
    )
    out = BASE / pdf_name
    subprocess.run(
        [EDGE, "--headless", "--disable-gpu", "--no-pdf-header-footer",
         f"--print-to-pdf={out}", html_path.as_uri()],
        check=True, capture_output=True,
    )
    html_path.unlink()
    print(f"{md_name} -> {pdf_name} ({out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    for md, pdf in JOBS:
        convert(md, pdf)
