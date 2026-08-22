"""P3-1 需求细化闭环:run HALT/歧义时的提问-答复-重规划通道。

设计(ADR-0009 §P3-1):
- refine 从 run 审计收集 open_questions + uncovered → 生成问题清单;
- 用户答复后 IR 增量更新(apply_answers,revision+1);
- uncovered 功能自动二次检索(换查询词重试一轮),命中块进重 plan 候选;
- 重 plan 复用 M3(make_plan),不改落图通道。
"""

from __future__ import annotations

import json
from pathlib import Path

from edaloop.intent.ir import DesignIR


def collect_questions(audit_dir: str) -> list[dict]:
    """run 审计 → 问题清单(open_questions 逐条 + uncovered 逐条转问题)。"""
    af = Path(audit_dir) / "audit.jsonl"
    events = [json.loads(l) for l in af.read_text(encoding="utf-8").splitlines() if l.strip()]
    questions: list[dict] = []
    seen_q: set[str] = set()
    for ev in events:
        k = ev.get("kind")
        if k == "ir":
            ir = ev.get("ir") or {}
            for q in ir.get("open_questions", []):
                key = q.get("question", "")
                if key and key not in seen_q:
                    seen_q.add(key)
                    questions.append(
                        {"id": q.get("id", f"Q{len(questions)+1}"), "question": key, "options": q.get("options", []), "source": "open_question"}
                    )
        elif k == "critic":
            # P4-4④ critic 闭环:findings → refine questions(采纳补件=按建议重规划/人工忽略)
            for f in ev.get("findings") or []:
                key = f.get("evidence", "")[:150]
                if key and key not in seen_q:
                    seen_q.add(key)
                    questions.append(
                        {
                            "id": f"C{len(questions)+1}",
                            "question": f"critic 建议: [{f.get('code', 'CRITIC')}] {key}",
                            "options": ["采纳补件(按建议重规划)", "人工忽略"],
                            "source": "critic",
                        }
                    )
        elif k == "round-validate":
            # P4-4③→④:PARAM_OFF_SPEC 弱观察进问题队列(weak/weak_codes 同序)
            for code, text in zip(ev.get("weak_codes") or [], ev.get("weak") or []):
                if code != "PARAM_OFF_SPEC" or not text:
                    continue
                if text[:150] in seen_q:
                    continue
                seen_q.add(text[:150])
                questions.append(
                    {
                        "id": f"P{len(questions)+1}",
                        "question": f"参数偏离建议值: {text[:150]}",
                        "options": ["采纳建议值(补/换标准件重规划)", "人工忽略"],
                        "source": "param-off-spec",
                    }
                )
        elif k == "round-plan":
            for i, u in enumerate(ev.get("uncovered") or [], 1):
                key = f"U{ev.get('round_no', 1)}-{i}: {u}"
                if u and key not in seen_q:
                    seen_q.add(key)
                    questions.append(
                        {"id": f"U{len(questions)+1}", "question": f"未覆盖功能如何处理: {u[:100]}", "options": ["接受缺口(仅交付已覆盖部分)", "补充需求细节/换方案"], "source": "uncovered"}
                    )
    return questions


_REWRITE_HINTS = (
    ("led", "LED 指示灯 限流电阻 指示"),
    ("端子", "接线端子 连接器 电源端子"),
    ("测试点", "测试点 探针 调试"),
    ("排针", "排针 排母 扩展 调试口"),
    ("电阻", "电阻 限流 上拉"),
    ("电容", "电容 去耦 滤波"),
    ("保护", "TVS 保险丝 浪涌 保护"),
)


def retry_query(uncovered_text: str) -> str:
    """uncovered → 二次检索查询词(同义词扩展,换措辞重试)。"""
    low = uncovered_text.lower()
    parts = [uncovered_text[:60]]
    for key, words in _REWRITE_HINTS:
        if key in low:
            parts.append(words)
    return " ".join(parts[:3])


def refine_run(
    audit_dir: str,
    answers: dict[str, str],
    ir_json_path: str | None = None,
) -> dict:
    """收集问题+应用答案 → 输出可重跑的 refine 包(IR v2 + 二次检索建议)。

    返回 {questions, applied, remaining, retry_queries, ir_path}。
    """
    questions = collect_questions(audit_dir)
    qmap = {q["id"]: q for q in questions}

    ir_path = Path(ir_json_path) if ir_json_path else Path(audit_dir) / "ir-v1.json"
    if not ir_path.exists():
        af = Path(audit_dir) / "audit.jsonl"
        for l in af.read_text(encoding="utf-8").splitlines():
            ev = json.loads(l)
            if ev.get("kind") == "ir":
                ir_path.parent.mkdir(parents=True, exist_ok=True)
                ir_path.write_text(json.dumps(ev.get("ir"), ensure_ascii=False), encoding="utf-8")
                break
    ir = DesignIR.model_validate_json(ir_path.read_text(encoding="utf-8"))

    applied = ir.apply_answers(answers)
    v2 = Path(audit_dir) / "ir-v2.json"
    v2.write_text(ir.model_dump_json(), encoding="utf-8")

    retry = []
    for q in questions:
        if q["source"] == "uncovered" and answers.get(q["id"], "").startswith("补充"):
            retry.append({"qid": q["id"], "query": retry_query(q["question"])})

    v2 = Path(audit_dir) / "ir-v2.json"
    v2.write_text(ir.model_dump_json(), encoding="utf-8")
    meta = Path(audit_dir) / "refine-meta.json"
    meta.write_text(
        json.dumps({"retry_queries": retry, "applied": applied, "ir": str(v2)}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    return {
        "questions": questions,
        "applied": applied,
        "remaining": [q["id"] for q in questions if q["id"] not in answers],
        "retry_queries": retry,
        "ir_path": str(v2),
        "ir_revision": ir.revision,
    }
