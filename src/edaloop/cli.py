from __future__ import annotations

import argparse
import json
import os
import sys
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
    p_run.add_argument("--max-rounds", type=int, default=5)
    p_run.add_argument("--dry-run", action="store_true", help="只跑 plan+validate,不落图(无 EasyEDA)")

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

    p_plan = sub.add_parser("plan", help="M3a:需求 → DesignIR → 检索 → BlockPlan(不出图)")
    p_plan.add_argument("input", help="需求文件路径(md/txt)")
    p_plan.add_argument("--out", default=None, help="BlockPlan 输出路径(默认 runs/plan-<id>.json)")
    p_plan.add_argument("--top-k", type=int, default=12)
    p_plan.add_argument("--db", default=None)

    p_apply = sub.add_parser("apply", help="M3b:BlockPlan → block-apply 落图 → sch gate(真机)")
    p_apply.add_argument("plan", help="plan 命令产出的 BlockPlan JSON 路径")
    p_apply.add_argument("--seeds", default="seeds/blocks.jsonl")

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
    if args.subset == "w3-loop":
        from edaloop.evals_w3 import run_w3_loop_eval

        summary = run_w3_loop_eval()
        return 0 if summary["go3"] and summary["go5"] else 1
    raise NotImplementedError(f"eval subset '{args.subset}' 尚未实现")


def _cmd_plan(args: argparse.Namespace) -> int:
    from edaloop.generate.pipeline import stage_plan

    md = Path(args.input).read_text(encoding="utf-8")
    ir, plan = stage_plan(
        md,
        source=Path(args.input).name,
        db_path=_db_path(args.db),
        top_k=args.top_k,
    )
    out = args.out or f"runs/plan-{plan.id}.json"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    print(f"plan {plan.id}: {len(plan.blocks)} blocks, confidence={plan.confidence}")
    for b in plan.blocks:
        print(f"  - {b.instance}: {b.block_id} -> {b.upstream_id}")
    for line in plan.provenance:
        print(f"  note: {line}")
    print(f"saved -> {out}")
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    from edaloop.generate.models import BlockPlan
    from edaloop.generate.pipeline import load_catalog, stage_apply

    plan = BlockPlan.model_validate_json(Path(args.plan).read_text(encoding="utf-8"))
    summary = stage_apply(plan, catalog=load_catalog(args.seeds))
    print(f"gate verdict: {summary['gate_verdict']}")
    for r in summary["results"]:
        if r["kind"] == "block-apply":
            print(f"  apply {r.get('instance')}: {r.get('status')}")
    if summary["apply_failures"]:
        print(f"apply failures: {len(summary['apply_failures'])}")
    print(f"audit -> {summary['audit_dir']}")
    return 0 if summary["gate_verdict"] == "pass" and not summary["apply_failures"] else 1


def _cmd_run(args: argparse.Namespace) -> int:
    from edaloop.generate.pipeline import stage_run

    if args.input == "-":
        md = sys.stdin.read()
        source = "stdin"
    else:
        md = Path(args.input).read_text(encoding="utf-8")
        source = Path(args.input).name
    body = md.split("## 期望指标")[0]
    ir, result = stage_run(
        body, source=source, max_rounds=args.max_rounds, dry_run=args.dry_run
    )
    print(f"run {ir.id}: status={result.status}")
    for r in result.rounds:
        blocking = [f for f in r.findings if not f.weak]
        print(
            f"  round {r.round_no}: gate={r.gate_verdict} blocking={len(blocking)}"
            + (f" halted={r.halted}" if r.halted else "")
        )
    if result.status == "PASS" and result.converged_round:
        print(f"converged in {result.converged_round} round(s)")
    print(f"audit -> {result.audit_dir}")
    return 0 if result.status == "PASS" else 1


def _cmd_ingest(args: argparse.Namespace) -> int:
    from edaloop.ingest.pipeline import ingest_pdf
    from edaloop.llm.openai_compat import get_llm

    for pdf in args.pdf:
        try:
            table, report = ingest_pdf(pdf, get_llm())
        except Exception as e:
            print(f"{pdf}: FAILED - {e}")
            return 1
        print(
            f"{pdf}: {table.part} pins={report.pin_count} pages={report.evidence_pages} "
            f"llm/rule={report.llm_pins}/{report.rule_pins} verdict={report.verdict}"
        )
        for d in report.disagreements[:5]:
            print(f"  disagree: {d}")
        for v in report.internal_violations[:5]:
            print(f"  violation: {v}")
    return 0


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
    if args.command == "plan":
        return _cmd_plan(args)
    if args.command == "apply":
        return _cmd_apply(args)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "ingest":
        return _cmd_ingest(args)
    raise NotImplementedError(f"command '{args.command}' 尚未实现")


if __name__ == "__main__":
    raise SystemExit(main())
