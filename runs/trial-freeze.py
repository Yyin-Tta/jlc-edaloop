# -*- coding: utf-8 -*-
"""算法分步目检(用户验证序)。

用法:
  ./.venv/Scripts/python.exe runs/trial-freeze.py <req.md> [pack]

模式:
  (默认) 第 1 步:跑到"试放+量框"为止,P1 画每块虚线框+左上坐标/长宽标注,冻结。
  pack     第 2 步:另做装箱(gap 200,填满整 BAND),把第 1 页的块真实落到 P2,
           P2 画框+标注+BAND 参考框(绿)后冻结;P1 试放框同时保留可对比。

看完手工清理:easyeda sch clear --doc P1 / --doc P2。
"""
import os
import sys

os.environ["EDALOOP_LAYOUT_FREEZE"] = "pack" if len(sys.argv) > 2 and sys.argv[2] == "pack" else "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from edaloop.generate.pipeline import stage_run  # noqa: E402

REQ_DIR = os.path.join(os.path.dirname(__file__), "..", "evals", "requirements")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    name = sys.argv[1]
    md = open(os.path.join(REQ_DIR, name), encoding="utf-8").read()
    body = md.split("## 期望指标")[0]
    _ir, result = stage_run(body, source=name, max_rounds=1)
    print(f"\n[trial-freeze] status={result.status} audit={result.audit_dir}")
    print("[trial-freeze] 页面已冻结,到 EasyEDA 检查;看完 sch clear --doc P1 / --doc P2 清理")
    return 0 if result.status == "FREEZE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
