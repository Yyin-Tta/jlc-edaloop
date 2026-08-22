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


_GO_RATE = 0.92  # P4-6 Go 判据(扩标注集);Stretch 0.95。旧 5 需求集 80% 线作废。
# 泛功能块:查询文本里没有型号抓手、只靠功能语义才能召回的金标(rail 通道/意图槽的主战场)。
# miss 单独归因计数,与型号件 miss分开看(P4-6 Go 判据要求)。
_GENERIC_GOLD = {
    "ldo-ams1117-3v3",
    "led-indicator",
    "usb-c-power-entry",
    "dc-terminal-5v-input",
    "dc-terminal-wide-input",
    "charger-tp4056",
    "boost-mt3608",
    "low-battery-alarm-tl431",
}


def run_w1_retrieval_eval(db_path: str = "runs/eval-w1.db") -> tuple[float, dict[str, list[str]]]:
    blocks = [
        BlockRecord.model_validate(json.loads(l))
        for l in _REPOS["seeds"].read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    annotations = json.loads(_REPOS["annotations"].read_text(encoding="utf-8"))
    llm = get_llm(temperature=0.0)  # 评测 IR 温度置 0:IR 文本方差会把边界块(端子/buck)在 92% 线上打摆
    store = KnowledgeStore(db_path, get_embedder(), get_reranker())
    store.rebuild(blocks)
    total = 0
    hits = 0
    generic_misses: list[str] = []
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
        generic_misses.extend(
            f"{req_file}:{e}" for e in expected if not equiv_hit(e) and e in _GENERIC_GOLD
        )
        detail[req_file] = [f"[{ir_ok}] {hit_n}/{len(expected)}"] + [
            f"{'HIT ' if equiv_hit(e) else 'MISS'} {e}" for e in expected
        ] + [f"top8={top_ids}"]
        print(f"\n== {req_file} ==")
        for line in detail[req_file]:
            print("  " + line)
    # P4-6 Go 验收:注入电压不兼容负样本(机械断言,无 LLM)——24V 直入设计里
    # ldo-ams1117-3v3(vmax 15V,VIN5 无 24V 落点)必须被 elec-deny 挤出 top8,
    # 即 planner 候选里不存在 → plan 无法选中;5V 常规设计不误伤(正控)。
    neg = store.retrieve("24V 工业电源直接输入,降压到 3.3V 供 MCU", top_k=8)
    neg_ldo = next((r for r in neg if r.block_id == "ldo-ams1117-3v3"), None)
    pos = store.retrieve("USB 5V 输入降压 3.3V 给单片机供电", top_k=8)
    pos_ldo = next((r for r in pos if r.block_id == "ldo-ams1117-3v3"), None)
    neg_ok = neg_ldo is None and pos_ldo is not None and "elec-deny" not in pos_ldo.channels
    detail["neg-elec"] = "ok" if neg_ok else "fail"
    print(f"\n负样本断言: 24V直入 ldo 出局={'是' if neg_ldo is None else f'否(rank{neg_ldo.rank})'}"
          f" / 5V设计 ldo 在位={'是' if pos_ldo else '否'} / 无误伤={'是' if pos_ldo and 'elec-deny' not in pos_ldo.channels else '否'}")
    store.close()
    rate = hits / total if total else 0.0
    print(f"\nrecall@8 = {hits}/{total} = {rate:.1%}  (Go >= {_GO_RATE:.0%})")
    if generic_misses:
        print(f"泛功能块 miss ({len(generic_misses)}): " + ", ".join(generic_misses))
    return rate, detail
