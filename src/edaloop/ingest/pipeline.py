from __future__ import annotations

from pathlib import Path

from edaloop.generate.audit import AuditLog
from edaloop.ingest.extract import llm_extract, llm_extract_suggestions, rule_channel
from edaloop.ingest.models import ElecRow, IngestReport, PinTable, Suggestion
from edaloop.ingest.pdf_pages import elec_rows, find_elec_pages, find_pin_pages, page_text
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
    # P4-6②/G16:电气参数表页 min/typ/max 机械提取(独立于引脚通道,失败不阻断引脚入库)
    elec: list[ElecRow] = []
    try:
        for ep in find_elec_pages(pdf_path)[:6]:
            for r in elec_rows(pdf_path, ep):
                elec.append(ElecRow.model_validate(r))
    except Exception as e:
        elec = []
        audit_kw = {"elec_error": str(e)[:120]}
    else:
        audit_kw = {"elec_rows": len(elec)}
    audit = AuditLog("runs/ingest")
    audit.event("ingest-start", pdf=pdf_name, pages=pages, **audit_kw)
    # 扩展候选页:pin 表常跨页(CH340C 表在页3-4),紧随 pin 页的下一页作为续表候选纳入;
    # 脚号重叠会在聚合阶段自然停止(MAX485 页7 DIP/SO vs 页8 µMAX 是同号不同封装,不合并)。
    from edaloop.ingest.pdf_pages import page_count as _page_count

    total_pages = _page_count(pdf_path)
    candidates: list[int] = []
    for p in pages:
        if p not in candidates:
            candidates.append(p)
        if p + 1 <= total_pages and p + 1 not in candidates:
            candidates.append(p + 1)

    # 多页 pin 聚合:候选页里可能有多张表(esp32-s3:Table 2-1 部分总览 / IO MUX 子表 /
    # Table 7-1 合并式完整总览)。按「脚号去重合并、重叠=新表」切成多张候选表,
    # 取脚数最多的一张(esp32-s3 Table 7-1 全 56 脚 > IO MUX 39 脚 > 部分总览 30 脚);
    # 并列取先出现者(保持既有单表「先出现即主体」语义,MAX485/ULN2003A 双封装同脚数不受影响)。
    runs: list[tuple[list, list[int], str | None]] = []
    cur_pins: list = []
    cur_pages: list[int] = []
    cur_part: str | None = None
    last_err: Exception | None = None
    for page_no in candidates:
        text = page_text(pdf_path, page_no)
        if len(text.strip()) < 200:
            continue
        try:
            t = llm_extract(text, pdf_name, llm, page_no)
        except Exception as e:
            last_err = e
            continue
        # ≥3 即采纳:3 脚器件(SOT-223/TO-92/SOT-23 LDO、晶体管)是真实形态;
        # 旧 ≥4 阈值把它们逼向「合并多封装凑数」(AMS1117 12 脚 run 2026-09-01)。
        if len(t.pins) < 3:
            continue
        if not cur_pins:
            cur_part = t.part
            cur_pins = list(t.pins)
            cur_pages = list(t.pages)
            continue
        # 续页合并守卫:真续表的新页脚号全是数字行(Table 3-1 cont'd 型);
        # 非数字号(GPIO0/GPIO46 等 strapping 脚表把脚名当号)与主表无交集也不并——
        # 无重叠只能证明不是重叠表,不能证明是续表(wroom 页 13 实测 41+4=45 超集)。
        all_digits = all(p.number.isdigit() for p in t.pins)
        if {p.number for p in cur_pins} & {p.number for p in t.pins} or not all_digits:
            runs.append((cur_pins, cur_pages, cur_part))
            cur_part = t.part
            cur_pins = list(t.pins)
            cur_pages = list(t.pages)
            continue
        cur_pins.extend(t.pins)
        cur_pages = sorted(set(cur_pages + t.pages))
    if cur_pins:
        runs.append((cur_pins, cur_pages, cur_part))
    if not runs:
        raise RuntimeError(f"{pdf_name}: 所有候选页提取失败: {last_err}")

    def _rule_rows(pages_: list[int]) -> int:
        n = 0
        for pno in pages_:
            n += len(rule_channel(page_text(pdf_path, pno), pno))
        return n

    # 取 run 的主键 = 规则通道可解析行数(机械可验证证据),脚数只作次键:
    # ① wroom-1 图页(Figure 3-1)LLM 能从散排标签重建全 41 脚但规则通道只出 7 条错位对,
    #    表页双通道 36 条 —— 旧「先出现者」并列判据把证据让给图页;
    # ② stm32f103c8 球图页(TFBGA64 ballout)LLM 重建 109 脚(无真实封装对应)
    #    胜过真表 48 脚列 —— 旧「脚数最多」在图重建 vs 真表对抗时选出不可验证的超集;
    # 证据等级 = ground truth(可机械解析)优先于 LLM 输出(核心设计原则 4);
    # 规则通道全军覆没时(esp32-s3 芯片表/纯图件)退化为脚数最多,仍并列取先出现。
    best = max(range(len(runs)), key=lambda i: (_rule_rows(runs[i][1]), len(runs[i][0]), -i))
    merged_pins, merged_pages, part_name = runs[best]

    table = PinTable(
        part=part_name or pdf_name,
        source_pdf=pdf_name,
        pages=merged_pages,
        pins=merged_pins,
        elec=elec,
    )
    # 规则通道同口径聚合(各被采纳页)
    rule: list = []
    for pno in merged_pages:
        rule.extend(rule_channel(page_text(pdf_path, pno), pno))
    report = run_gate(table, rule)
    try:
        from edaloop.ingest.pdf_pages import page_count

        front = [p for p in range(1, min(7, page_count(pdf_path)) + 1) if p not in merged_pages][:5]
        extra = next((p for p in pages if p not in merged_pages), None)
        for sp in sorted({p for p in [*front, extra] if p}):
            for s in llm_extract_suggestions(page_text(pdf_path, sp), llm, sp):
                report.suggestions.append(Suggestion.model_validate(s))
    except Exception as e:
        audit.event("suggestions-error", pdf=pdf_name, error=str(e)[:200])
    audit.event(
        "ingest-gate",
        pdf=pdf_name,
        page=merged_pages[0],
        verdict=report.verdict,
        llm=report.llm_pins,
        rule=report.rule_pins,
        disagreements=report.disagreements[:10],
        violations=report.internal_violations[:10],
        suggestions=len(report.suggestions),
    )
    if report.passed or report.verdict == "low-confidence":
        store = DatasheetStore(db_path)
        store.upsert(table, report)
        store.close()
    audit.event("ingest-done", pdf=pdf_name, verdict=report.verdict, part=table.part)
    return table, report
