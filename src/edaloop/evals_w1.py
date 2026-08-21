from __future__ import annotations

import json
from pathlib import Path

from edaloop.intent.ir import DesignIR
from edaloop.intent.parse import IRParseError, requirement_to_ir
from edaloop.knowledge.models import BlockRecord
from edaloop.knowledge.store import KnowledgeStore
from edaloop.llm.base import LLMProvider
from edaloop.llm.openai_compat import get_embedder, get_llm, get_reranker

_REPOS = {
    "seeds": Path("seeds/blocks.jsonl"),
    "annotations": Path("evals/w1-retrieval.json"),
    "requirements": Path("evals/requirements"),
    "archive": Path("evals/requirements/archive"),
}


def _req_path(req_file: str) -> Path:
    """标注可指向现行需求或 archive/ 裁撤件(v2 重设计后部分输入被归档,文本仍是有效检索输入)。"""
    p = _REPOS["requirements"] / req_file
    return p if p.exists() else _REPOS["archive"] / req_file


def _customer_voice(md: str) -> str:
    body = md.split("## 期望指标")[0]
    lines = [l for l in body.splitlines() if not l.strip().startswith(">")]
    return "\n".join(lines).strip()


def _parse_ir(md: str, llm: LLMProvider, source: str) -> DesignIR:
    last_err: Exception | None = None
    for _ in range(2):
        try:
            return requirement_to_ir(md, llm, source=source)
        except IRParseError as e:
            last_err = e
    raise RuntimeError(f"DesignIR 解析连续失败: {last_err}")


def run_w1_retrieval_eval(db_path: str = "runs/eval-w1.db") -> tuple[float, dict[str, list[str]]]:
    blocks = [
        BlockRecord.model_validate(json.loads(l))
        for l in _REPOS["seeds"].read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    annotations = json.loads(_REPOS["annotations"].read_text(encoding="utf-8"))
    llm = get_llm()
    store = KnowledgeStore(db_path, get_embedder(), get_reranker())
    store.rebuild(blocks)
    total = 0
    hits = 0
    detail: dict[str, list[str]] = {}
    for req_file, expected in annotations.items():
        if req_file in ("metric", "note"):
            continue
        md = _req_path(req_file).read_text(encoding="utf-8")
        body = _customer_voice(md)
        try:
            ir = _parse_ir(body, llm, source=req_file)
            query = ir.query_text()
            ir_ok = "ir"
        except Exception as e:
            query = body
            ir_ok = f"raw-fallback({type(e).__name__})"
        results = store.retrieve(query, top_k=8)
        top_ids = [r.block_id for r in results]
        top_set = set(top_ids)

        EQUIV = {
            "usb-c-power-entry": {"usb-c-power-entry", "usb-c-16p", "up-usbc_dual_orientation_data"},
            "dc-terminal-5v-input": {"dc-terminal-5v-input", "terminal-kf301-2p"},
        }

        def equiv_hit(block: str) -> bool:
            return bool(EQUIV.get(block, {block}) & top_set)

        hit_n = sum(1 for e in expected if equiv_hit(e))
        total += len(expected)
        hits += hit_n
        detail[req_file] = [f"[{ir_ok}] {hit_n}/{len(expected)}"] + [
            f"{'HIT ' if equiv_hit(e) else 'MISS'} {e}" for e in expected
        ] + [f"top8={top_ids}"]
        print(f"\n== {req_file} ==")
        for line in detail[req_file]:
            print("  " + line)
    store.close()
    rate = hits / total if total else 0.0
    print(f"\nrecall@8 = {hits}/{total} = {rate:.0%}  (Go >= 80%)")
    return rate, detail
