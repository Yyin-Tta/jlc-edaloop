from __future__ import annotations

import argparse

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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    raise NotImplementedError(f"command '{args.command}' 尚未实现(当前为 M0 骨架)")


if __name__ == "__main__":
    raise SystemExit(main())
