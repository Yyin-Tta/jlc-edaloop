"""P3-1 需求细化闭环:run HALT/歧义时的提问-答复-重规划通道。

设计(ADR-0009 §P3-1 + P4-5③ 深化):
- refine 从 run 审计收集 open_questions + critic/参数弱观察 + uncovered → 问题清单;
  **编号单源化(P4-5③)**:Q 用 IR 原生 id,C/P/U 由问题内容哈希派生——与清单位置
  解耦,两次 collect_questions 之间问题增删不会错位(按位置编号的旧实现实测会串答案);
- 用户答复后 IR 增量更新(apply_answers,revision+1);**U/C/P 答案进 decisions
  双注入(P4-5③)**:decisions(planner/refine 摘要可见)+ uncovered 答案另产
  二次检索词(检索侧可见);
- uncovered 功能自动二次检索(**embedding 近邻**换查询词,复用 get_embedder;
  无 embedder 密钥时落回静态同义词表),命中块进重 plan 候选;
- 重 plan 复用 M3(make_plan),不改落图通道。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from edaloop.intent.ir import DesignIR


def _stable_id(prefix: str, text: str) -> str:
    """内容哈希 id(单源):同一问题文本在任何清单位置得到同一 id。"""
    return f"{prefix}-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:6]}"


def collect_questions(audit_dir: str) -> list[dict]:
    """run 审计 → 问题清单(open_questions 逐条 + critic/参数弱观察 + uncovered 逐条转问题)。"""
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
                        {"id": q.get("id", "") or _stable_id("Q", key), "question": key,
                         "options": q.get("options", []), "source": "open_question"}
                    )
        elif k == "critic":
            # P4-4④ critic 闭环:findings → refine questions(采纳补件=按建议重规划/人工忽略)
            for f in ev.get("findings") or []:
                key = f.get("evidence", "")[:150]
                if key and key not in seen_q:
                    seen_q.add(key)
                    questions.append(
                        {
                            "id": _stable_id("C", key),
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
                        "id": _stable_id("P", text[:150]),
                        "question": f"参数偏离建议值: {text[:150]}",
                        "options": ["采纳建议值(补/换标准件重规划)", "人工忽略"],
                        "source": "param-off-spec",
                    }
                )
        elif k == "round-plan":
            for u in ev.get("uncovered") or []:
                if not u:
                    continue
                if f"U: {u[:100]}" in seen_q:
                    continue
                seen_q.add(f"U: {u[:100]}")
                questions.append(
                    {
                        "id": _stable_id("U", u[:100]),
                        "question": f"未覆盖功能如何处理: {u[:100]}",
                        "options": ["接受缺口(仅交付已覆盖部分)", "补充需求细节/换方案"],
                        "source": "uncovered",
                    }
                )
    return questions


# 静态同义词表:无 embedding 密钥时的降级通道(有 embedder 时不用)
_REWRITE_HINTS = (
    ("led", "LED 指示灯 限流电阻 指示"),
    ("端子", "接线端子 连接器 电源端子"),
    ("测试点", "测试点 探针 调试"),
    ("排针", "排针 排母 扩展 调试口"),
    ("电阻", "电阻 限流 上拉"),
    ("电容", "电容 去耦 滤波"),
    ("保护", "TVS 保险丝 浪涌 保护"),
)


_VECCACHE: dict[tuple, list[list[float]]] = {}


def retry_query(uncovered_text: str, embedder=None, vocab: list[str] | None = None) -> str:
    """uncovered → 二次检索查询词。

    P4-5③:embedding 近邻优先——把 uncovered 文本和块词汇表(块名+标签)各自嵌入,
    取最近邻块词换措辞重试(同义表达的召回远好于静态表);无 embedder 落回
    _REWRITE_HINTS。返回值始终含原文前缀,检索通道照旧。词汇表向量进程内缓存,
    多个 uncovered 不重复嵌入。
    """
    # 剥离 collect_questions 的固定前缀(只贡献「功能/覆盖」这类全库噪音 trigram)
    core = re.sub(r"^未覆盖功能如何处理[:：]\s*", "", uncovered_text)
    base = core[:40]  # 短基底:减少叙述文噪音词,提示词权重不被稀释
    if embedder is not None and vocab:
        try:
            import math

            top = vocab[:400]
            ck = (id(embedder), len(top), top[0] if top else "")
            if ck not in _VECCACHE:
                _VECCACHE[ck] = embedder.embed_documents(top)
            q = embedder.embed_query(base)
            vecs = _VECCACHE[ck]

            def _cos(a: list[float], b: list[float]) -> float:
                dot = sum(x * y for x, y in zip(a, b))
                na = math.sqrt(sum(x * x for x in a)) or 1.0
                nb = math.sqrt(sum(x * x for x in b)) or 1.0
                return dot / (na * nb)

            ranked = sorted(zip(top, vecs), key=lambda kv: -_cos(q, kv[1]))
            near = [w for w, v in ranked[:3] if _cos(q, v) > 0.35]
            if near:
                return " ".join([base] + near)
        except Exception:
            pass  # embedding 失败落回静态表,不阻断 refine
    low = uncovered_text.lower()
    parts = [base]
    # 提示词连写成一个长 CJK 段:trigram FTS 对 <3 字连续段零索引,
    # 按空格分词会让「电阻/限流/上拉」这类两字提示词整体失效(实测电阻块掉出 top12)。
    hints = "".join(words for key, words in _REWRITE_HINTS if key in low).replace(" ", "")
    if hints:
        parts.append(hints)
    return " ".join(parts)


def _block_vocab(seeds_path: str | Path = "seeds/blocks.jsonl") -> list[str]:
    """块词汇表(块名 + tags),embedding 近邻的候选词空间。"""
    p = Path(seeds_path)
    if not p.exists():
        return []
    vocab: list[str] = []
    for l in p.read_text(encoding="utf-8").splitlines():
        if not l.strip():
            continue
        try:
            r = json.loads(l)
        except json.JSONDecodeError:
            continue
        if r.get("name"):
            vocab.append(r["name"])
        vocab.extend(t for t in (r.get("tags") or []) if t)
    return vocab


def _get_embedder():
    """有嵌入密钥才构造;单测隔离(PYTEST_CURRENT_TEST)——开发 shell 常驻密钥,
    不隔离会让单测真调嵌入 API(实测全量从 3s 拖到分钟级)。"""
    import os

    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    if not (os.environ.get("EDALOOP_EMBED_KEY") or os.environ.get("OPENAI_API_KEY")):
        return None
    try:
        from edaloop.llm.openai_compat import get_embedder

        return get_embedder()
    except Exception:
        return None


def refine_run(
    audit_dir: str,
    answers: dict[str, str],
    ir_json_path: str | None = None,
) -> dict:
    """收集问题+应用答案 → 输出可重跑的 refine 包(IR v2 + 二次检索建议)。

    返回 {questions, applied, manual_applied, remaining, retry_queries, ir_path}。
    P4-5③:U/C/P 答案(不在 IR open_questions 里的)进 decisions 双注入——
    decisions 摘要随 planner 查询下发,uncovered 答案另产 embedding 近邻检索词。
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
    # P4-5③ 双注入之一:非 open_question 的答案(U/C/P)也进 decisions——
    # planner 经 decisions_digest 看到,不再只影响检索词。
    manual = {qid: answers[qid] for qid in answers if qid in qmap and answers[qid]}
    if manual:
        decided = dict(ir.decisions or {})
        decided.update(manual)
        ir.decisions = decided
        if not applied:
            ir.revision += 1
    v2 = Path(audit_dir) / "ir-v2.json"
    v2.write_text(ir.model_dump_json(), encoding="utf-8")

    embedder = _get_embedder()
    vocab = _block_vocab() if embedder is not None else None
    retry = []
    for q in questions:
        if q["source"] == "uncovered" and answers.get(q["id"], "").startswith("补充"):
            retry.append({"qid": q["id"], "query": retry_query(q["question"], embedder, vocab)})

    meta = Path(audit_dir) / "refine-meta.json"
    meta.write_text(
        json.dumps({"retry_queries": retry, "applied": applied, "manual_applied": len(manual),
                    "ir": str(v2)}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    return {
        "questions": questions,
        "applied": applied,
        "manual_applied": len(manual),
        "remaining": [q["id"] for q in questions if q["id"] not in answers],
        "retry_queries": retry,
        "ir_path": str(v2),
        "ir_revision": ir.revision,
    }
