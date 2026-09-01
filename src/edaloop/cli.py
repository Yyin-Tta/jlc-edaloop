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
    p_run.add_argument("--answers", default=None, help="questions 答案文件(JSON: {Q1: 'A 方案...', ...})回灌主链路")
    p_run.add_argument("--ir", default=None, help="refine 产出的 IR-v2 JSON 路径(跳过解析,直接用增量 IR 跑)")

    p_ingest = sub.add_parser("ingest", help="M6:datasheet PDF 入库(提取 + 交叉校验)")
    p_ingest.add_argument("pdf", nargs="+", help="datasheet PDF 路径(可多个)")

    p_eval = sub.add_parser("eval", help="跑 evals 金标准集")
    p_eval.add_argument("--subset", default=None, help="子集:w1-retrieval / w3-loop")
    p_eval.add_argument("--tier", default=None, help="w3-loop 层级:easy(4)/medium(5)/hard(5) 难度层,smoke(3~12min)/daily(8)/rest(全量减 daily,发版增量) 回归级,all(真全量重跑);electrical(P4-3 注入式电气缺陷样本);params(P4-4 参数核对闭环:错值拦截+电源块覆盖+critic 捕获);refine(P4-5 验收规格+功能覆盖+refine 转化);都不走 E2E")

    p_q = sub.add_parser("questions", help="弱门禁确认队列:DesignIR open_questions + uncovered 项")
    p_q.add_argument("input", help="需求文件路径(md/txt)")
    p_q.add_argument("--plan", default=None, help="BlockPlan JSON(供 uncovered 列表)")
    p_q.add_argument("--answer", default=None, help="答案文件(JSON: {Q1: 'A', ...}),省略则交互式提问")

    p_rp = sub.add_parser("replay", help="按审计日志重放最终轮的落图动作(不重算 LLM)")
    p_rp.add_argument("audit_dir", help="run 的审计目录(如 runs/run-xxxx)")
    p_rp.add_argument("--dry-run", action="store_true", help="只统计可重放动作,不落图")

    p_rf = sub.add_parser("refine", help="P3-1 需求细化:从 run 审计收集问题→应用答案→IR-v2+二次检索建议")
    p_rf.add_argument("audit_dir", help="run 的审计目录(如 runs/run-xxxx)")
    p_rf.add_argument("--answers", default=None, help="答案文件(JSON: {Q1: '...', U1: '补充...'})")
    p_rf.add_argument("--list", action="store_true", help="只列问题清单,不应用答案")

    p_pcb = sub.add_parser("pcb", help="P3-5 PCB 编排:当前工程 sch→PCB→布局布线→门禁(需已打开工程)")
    p_pcb.add_argument("--no-mount-holes", action="store_true", help="跳过 M3 安装孔")
    p_pcb.add_argument("--no-retry", action="store_true", help="跳过 drc 违规重布环")

    p_qt = sub.add_parser("quote", help="P3-6 报价:BOM 预检+三段报价(PCB/SMT/元件)")
    p_qt.add_argument("bom", help="delivery.bom.json 路径")
    p_qt.add_argument("--layers", type=int, default=2, choices=[2, 4])
    p_qt.add_argument("--qty", type=int, default=5)
    p_qt.add_argument("--order-draft", action="store_true", help="同时生成订单草稿(仍未提交/无支付)")

    p_od = sub.add_parser("order", help="P3-6 订单草稿生成(显式确认;支付永不做)")
    p_od.add_argument("bom", help="delivery.bom.json 路径")
    p_od.add_argument("--confirm", action="store_true", help="显式确认生成订单草稿")
    p_od.add_argument("--out", default="runs/order", help="输出目录")

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

    p_ui = sub.add_parser("ui", help="Chainlit Web UI:聊天+上传需求/datasheet,流式看 run 进度(需 uv sync --extra ui)")
    p_ui.add_argument("--host", default="127.0.0.1")
    p_ui.add_argument("--port", type=int, default=8000)
    p_ui.add_argument("--headless", action="store_true", help="不自动开浏览器")

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
    # P4-6③:种子案例随块库装载(cases 表 rebuild 不清空,record_case hash 去重幂等;
    # writeback 写入的 run 案例不受影响)
    from edaloop.knowledge.models import CaseRecord

    cases_path = Path(args.seeds).parent / "cases.jsonl"
    n_cases = 0
    if cases_path.exists():
        for line in cases_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                n_cases += int(store.record_case(CaseRecord.model_validate_json(line)))
    store.close()
    print(f"seeded {n} blocks + {n_cases} cases -> {_db_path(args.db)}")
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
        from edaloop.evals_w1 import _GO_RATE, run_w1_retrieval_eval

        rate, detail = run_w1_retrieval_eval()
        # Go = recall 达线 且 负样本机械断言通过(24V 直入 ldo 出局 + 5V 正控不误伤)
        return 0 if rate >= _GO_RATE and detail.get("neg-elec") == "ok" else 1
    if args.subset == "w3-loop":
        if args.tier == "electrical":
            # P4-3 注入式电气缺陷 harness(不走 E2E;14 需求零误伤由 smoke/daily 回归实证)
            from edaloop.evals_electrical import run_electrical_eval

            summary = run_electrical_eval()
            return 0 if summary["go"] else 1
        if args.tier == "params":
            # P4-4 参数核对闭环 harness(错值拦截/干净零误杀/电源块覆盖/来源表/critic)
            from edaloop.evals_params import run_params_eval

            summary = run_params_eval()
            return 0 if summary["go"] else 1
        if args.tier == "refine":
            # P4-5 验收规格/功能覆盖/refine 闭环 harness(生成率/可执行率/注入/零误伤/转化)
            from edaloop.evals_refine import run_refine_eval

            summary = run_refine_eval()
            return 0 if summary["go"] else 1
        from edaloop.evals_w3 import run_w3_loop_eval

        summary = run_w3_loop_eval(tier=args.tier)
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
    body = md  # P4-5①:「## 期望指标」段不再丢弃(pipeline 解析为验收条目,IR parse 亦可见)
    answers = None
    if args.answers:
        answers = json.loads(Path(args.answers).read_text(encoding="utf-8"))
    retry_queries = None
    ir_path = args.ir
    if ir_path:
        rf = Path(ir_path).parent / "refine-meta.json"
        if rf.exists():
            meta = json.loads(rf.read_text(encoding="utf-8"))
            retry_queries = meta.get("retry_queries")
    ir, result = stage_run(
        body,
        source=source,
        max_rounds=args.max_rounds,
        dry_run=args.dry_run,
        answers=answers,
        ir_path=ir_path,
        retry_queries=[r.get("query") for r in (retry_queries or []) if r.get("query")],
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
    lr = json.loads((Path(result.audit_dir) / "loop-result.json").read_text(encoding="utf-8"))
    for k, v in (lr.get("delivery") or {}).items():
        print(f"  deliver {k}: {v}")
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
        for s in report.suggestions[:14]:
            print(f"  suggest[{s.kind}] p{s.page}: {s.text}  «{s.quote[:50]}»")
    return 0


def _cmd_questions(args: argparse.Namespace) -> int:
    import sys as _sys

    from edaloop.intent.ir import DesignIR
    from edaloop.intent.parse import requirement_to_ir
    from edaloop.llm.openai_compat import get_llm

    md = Path(args.input).read_text(encoding="utf-8")
    body = md  # P4-5①:标注段不再丢弃
    answers: dict[str, str] = {}
    if args.answer:
        answers = json.loads(Path(args.answer).read_text(encoding="utf-8"))

    questions: list[tuple[str, str, list[str]]] = []
    try:
        ir = requirement_to_ir(body, get_llm(), source=Path(args.input).name)
        Path("runs").mkdir(exist_ok=True)
        Path("runs/last-ir.json").write_text(ir.model_dump_json(indent=2), encoding="utf-8")
        questions += [(q.id, q.question, q.options) for q in ir.open_questions]
    except Exception as e:
        print(f"(DesignIR 解析失败,跳过 open_questions: {type(e).__name__})", file=_sys.stderr)

    if args.plan:
        from edaloop.generate.models import BlockPlan

        plan = BlockPlan.model_validate_json(Path(args.plan).read_text(encoding="utf-8"))
        for i, item in enumerate(plan.uncovered, 1):
            questions.append((f"U{i}", f"未覆盖项: {item[:120]}", ["确认接受(知识库缺口)", "人工补块"]))

    if not questions:
        print("无需确认的弱门禁项。")
        return 0

    out_path = Path("runs/confirmations.json")
    resolved: dict[str, str] = dict(answers)
    print(f"弱门禁确认队列({len(questions)} 项)")
    for qid, text, options in questions:
        print(f"\n[{qid}] {text}")
        for i, opt in enumerate(options, 1):
            print(f"  {i}. {opt}")
        if qid in answers:
            print(f"  -> 已由答案文件给定: {answers[qid]}")
            continue
        while True:
            raw = input("选择编号(回车=待定): ").strip()
            if not raw:
                resolved[qid] = "PENDING"
                break
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                resolved[qid] = options[int(raw) - 1]
                break
            print("  无效输入")
    out_path.write_text(json.dumps(resolved, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n已保存 -> {out_path}")
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    from edaloop.replay import replay_run

    try:
        result = replay_run(args.audit_dir, dry_run=args.dry_run)
    except Exception as e:
        print(f"replay 失败: {e}")
        return 1
    print(
        f"replayed {result['replayed']} action(s) from round {result['final_round']} "
        f"-> gate {result['gate_verdict']}"
    )
    for err in result.get("errors", [])[:5]:
        print(f"  error: {err}")
    return 0 if result["gate_verdict"] == "pass" else 1


def _cmd_refine(args: argparse.Namespace) -> int:
    from edaloop.refine import collect_questions, refine_run

    if args.list:
        for q in collect_questions(args.audit_dir):
            opts = " | ".join(q["options"][:3])
            print(f"[{q['id']}:{q['source']}] {q['question'][:90]}")
            if opts:
                print(f"    选项: {opts}")
        return 0
    if not args.answers:
        print("需要 --answers(或 --list 只看问题清单)")
        return 2
    answers = json.loads(Path(args.answers).read_text(encoding="utf-8"))
    result = refine_run(args.audit_dir, answers)
    print(f"applied {result['applied']} answer(s); IR revision -> {result['ir_revision']}")
    if result["remaining"]:
        print(f"未答: {result['remaining']}")
    for r in result["retry_queries"]:
        print(f"  二次检索[{r['qid']}]: {r['query']}")
    print(f"IR-v2 -> {result['ir_path']}")
    print(f"重跑: uv run edaloop run <需求.md> --ir {result['ir_path']}")
    return 0


def _cmd_pcb(args: argparse.Namespace) -> int:
    from edaloop.generate.audit import AuditLog
    from edaloop.generate.pcb import stage_pcb

    audit = AuditLog("runs/pcb")
    result = stage_pcb(audit=audit, mount_holes=not args.no_mount_holes, retry=not args.no_retry)
    for s in result["steps"]:
        print(f"  {s['step']}: rc={s['rc']}")
    print(f"gate_ok: {result['gate_ok']}  degraded: {result['degraded']}")
    rep = Path("runs/pcb/pcb-report.md")
    rep.write_text(result["report"], encoding="utf-8")
    print(f"report -> {rep}")
    return 0 if result["gate_ok"] or result["degraded"] else 1


def _cmd_quote(args: argparse.Namespace) -> int:
    from edaloop.generate.ordering import order_draft, precheck_bom, quote

    pre = precheck_bom(args.bom)
    if not pre["ok"]:
        print(f"预检: {len(pre['problems'])} 项问题")
        for p in pre["problems"]:
            print(f"  {p['ref']}: {p['issue']} → {p['fix']}")
    else:
        print("预检: 通过")
    q = quote(args.bom, layers=args.layers, qty=args.qty)
    print(f"报价({args.layers}层 x{args.qty}): PCB ¥{q.pcb_cost:.2f} + SMT ¥{q.smt_cost:.2f} + 元件 ¥{q.parts_cost:.2f} = ¥{q.total:.2f}")
    for n in q.notes:
        print(f"  note: {n}")
    if args.order_draft:
        out = order_draft(q, "当前工程", out_dir="runs/order")
        print(f"订单草稿 -> {out}")
    return 0


def _cmd_order(args: argparse.Namespace) -> int:
    from edaloop.generate.ordering import order_draft, quote

    if not args.confirm:
        print("订单草稿含资金相关内容,需 --confirm 显式确认(本命令永不提交订单/支付)")
        return 2
    q = quote(args.bom)
    out = order_draft(q, "当前工程", out_dir=args.out)
    print(f"订单草稿 -> {out}")
    print("支付在嘉立创官方页面人工完成(edaloop 不做)")
    return 0


def _cmd_ui(args: argparse.Namespace) -> int:
    import subprocess

    try:
        import chainlit  # noqa: F401
    except ImportError:
        print("缺少 chainlit,先装:uv sync --extra ui")
        return 2
    app = Path(__file__).with_name("ui") / "app.py"
    cmd = [
        sys.executable, "-m", "chainlit", "run", str(app),
        "--host", args.host, "--port", str(args.port),
    ]
    if args.headless:
        cmd.append("--headless")
    print(f"starting: {' '.join(cmd)}")
    print("(工作目录须为仓库根:runs/ seeds/ 按相对路径解析;会话附件落 runs/ui/)")
    return subprocess.call(cmd)


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
    if args.command == "questions":
        return _cmd_questions(args)
    if args.command == "replay":
        return _cmd_replay(args)
    if args.command == "refine":
        return _cmd_refine(args)
    if args.command == "pcb":
        return _cmd_pcb(args)
    if args.command == "quote":
        return _cmd_quote(args)
    if args.command == "order":
        return _cmd_order(args)
    if args.command == "ui":
        return _cmd_ui(args)
    raise NotImplementedError(f"command '{args.command}' 尚未实现")


if __name__ == "__main__":
    raise SystemExit(main())
