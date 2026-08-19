from __future__ import annotations

from pathlib import Path

from edaloop.generate.audit import AuditLog
from edaloop.ingest.extract import llm_extract, llm_extract_suggestions, rule_channel
from edaloop.ingest.models import IngestReport, PinTable, Suggestion
from edaloop.ingest.pdf_pages import find_pin_pages, page_text
from edaloop.ingest.store import DatasheetStore
from edaloop.ingest.validate import run_gate
from edaloop.llm.base import LLMProvider


def ingest_pdf(
    pdf_path: str,
    llm: LLMProvider,
    *,
    db_path: str = "runs/knowledge.db",
) -> tuple[PinTable, IngestReport]:
    pdf_name = Path(pdf_path).name
    pages = find_pin_pages(pdf_path)
    if not pages:
        raise RuntimeError(f"{pdf_name}: 未找到引脚定义页(扫描件或非标准排版?)")
    audit = AuditLog("runs/ingest")
    audit.event("ingest-start", pdf=pdf_name, pages=pages)
    table: PinTable | None = None
    report: IngestReport | None = None
    last_err: Exception | None = None
    for page_no in pages:
        text = page_text(pdf_path, page_no)
        if len(text.strip()) < 200:
            continue
        try:
            table = llm_extract(text, pdf_name, llm, page_no)
        except Exception as e:
            last_err = e
            continue
        if len(table.pins) >= 4:
            rule = rule_channel(text, page_no)
            report = run_gate(table, rule)
            try:
                from edaloop.ingest.pdf_pages import page_count

                extra = next((p for p in pages if p != page_no), None)
                front = [p for p in range(1, min(7, page_count(pdf_path)) + 1) if p != page_no][:5]
                sug_pages = {p for p in [page_no, extra, *front] if p}
                for sp in sorted(sug_pages):
                    if sp == page_no:
                        continue
                    for s in llm_extract_suggestions(page_text(pdf_path, sp), llm, sp):
                        report.suggestions.append(Suggestion.model_validate(s))
            except Exception as e:
                audit.event("suggestions-error", pdf=pdf_name, error=str(e)[:200])
            audit.event(
                "ingest-gate",
                pdf=pdf_name,
                page=page_no,
                verdict=report.verdict,
                llm=report.llm_pins,
                rule=report.rule_pins,
                disagreements=report.disagreements[:10],
                violations=report.internal_violations[:10],
                suggestions=len(report.suggestions),
            )
            break
    if table is None or report is None:
        raise RuntimeError(f"{pdf_name}: 所有候选页提取失败: {last_err}")
    if report.passed or report.verdict == "low-confidence":
        store = DatasheetStore(db_path)
        store.upsert(table, report)
        store.close()
    audit.event("ingest-done", pdf=pdf_name, verdict=report.verdict, part=table.part)
    return table, report
