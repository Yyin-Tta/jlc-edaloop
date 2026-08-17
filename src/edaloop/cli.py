from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from edaloop import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="edaloop",
        description="嘉立创 EDA 智能原理图设计 agent:RAG 检索 + 两段式生成 + 机械校验闭环",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_run = sub.add_parser("run", help="M5 全链路:需求 → 检索 → 生成 → 校验迭代 → 交付")
    p_run.add_argument("input", help="需求文件路径(md/txt),或 '-' 表示 stdin")

    p_ingest = sub.add_parser("ingest", help="M6:datasheet PDF 入库(提取 + 交叉校验)")
    p_ingest.add_argument("pdf", nargs="+", help="datasheet PDF 路径(可多个)")

    p_eval = sub.add_parser("eval", help="跑 evals 金标准集")
    p_eval.add_argument("--subset", default=None, help="仅跑指定子集(如 w1-retrieval)")

    sub.add_parser("replay", help="按审计日志重放一轮迭代")

    p_seed = sub.add_parser("seed", help="M2:种子块库入库(全量重建,含向量索引)")
    p_seed.add_argument("--db", default=None, help="知识库路径(默认 EDALOOP_KB_PATH 或 runs/knowledge.db)")
    p_seed.add_argument("--seeds", default="seeds/blocks.jsonl", help="种子块 jsonl 路径")

    p_ret = sub.add_parser("retrieve", help="M2:混合检索(dense+BM25→RRF→rerank)")
    p_ret.add_argument("query", help="查询文本(需求/功能描述)")
    p_ret.add_argument("--top-k", type=int, default=5)
    p_ret.add_argument("--db", default=None, help="知识库路径(默认 EDALOOP_KB_PATH 或 runs/knowledge.db)")

    return parser


def _db_path(cli_value: str | None) -> str:
    return cli_value or os.environ.get("EDALOOP_KB_PATH", "runs/knowledge.db")


def _cmd_seed(args: argparse.Namespace) -> int:
    from edaloop.knowledge.models import BlockRecord
    from edaloop.knowledge.store import KnowledgeStore
    from edaloop.llm.openai_compat import get_embedder, get_reranker

    blocks = [
        BlockRecord.model_validate(json.loads(line))
        for line in Path(args.seeds).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    store = KnowledgeStore(_db_path(args.db), get_embedder(), get_reranker())
    n = store.rebuild(blocks)
    store.close()
    print(f"seeded {n} blocks -> {_db_path(args.db)}")
    return 0


def _cmd_retrieve(args: argparse.Namespace) -> int:
    from edaloop.knowledge.store import KnowledgeStore
    from edaloop.llm.openai_compat import get_embedder, get_reranker

    store = KnowledgeStore(_db_path(args.db), get_embedder(), get_reranker())
    results = store.retrieve(args.query, top_k=args.top_k)
    store.close()
    for r in results:
        print(
            f"{r.rank}. [{r.score:.4f}] {r.block_id} | {r.name} | channels={','.join(r.channels)}"
        )
    if not results:
        print("(no results)")
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    if args.subset in (None, "w1-retrieval"):
        from edaloop.evals_w1 import run_w1_retrieval_eval

        rate, _ = run_w1_retrieval_eval()
        return 0 if rate >= 0.8 else 1
    raise NotImplementedError(f"eval subset '{args.subset}' 尚未实现")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "seed":
        return _cmd_seed(args)
    if args.command == "retrieve":
        return _cmd_retrieve(args)
    if args.command == "eval":
        return _cmd_eval(args)
    raise NotImplementedError(f"command '{args.command}' 尚未实现(W1:seed/retrieve/eval 已可用)")


if __name__ == "__main__":
    raise SystemExit(main())
