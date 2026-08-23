from __future__ import annotations

import json
from pathlib import Path

from edaloop.generate.pipeline import stage_run

_REQ_DIR = Path("evals/requirements")
_REQS = sorted(p.name for p in _REQ_DIR.glob("req-*.md"))
# v2 重设计(26->14):编号即难度顺序 01-04 简单 / 05-09 中等 / 10-14 困难,覆盖矩阵见 evals/README.md
# 回归级从难度层派生:smoke 每层抽 1 最快(block 基线+自由拓扑+隔离历史难例);daily 保持 8 个规模≈32min
_TIER = {
    # 简单:单电源域/小 BOM/块直命中,预期 1 轮
    "easy": [
        "req-01-esp32s3-mini-2layer.md",
        "req-02-rs485-sensor-hub.md",
        "req-03-ir-remote-hub.md",
        "req-04-smart-dial-oled.md",
    ],
    # 中等:多块组合/电源树>=2级/自由拓扑/refine 歧义
    "medium": [
        "req-05-battery-ble-telemetry.md",
        "req-06-pico-native-usb-gateway.md",
        "req-07-motor-driver-board.md",
        "req-08-liion-protection-freeform.md",
        "req-09-ambiguous-sensor-node.md",
    ],
    # 困难:隔离/4层叠层/双MCU/多插槽母板/端到端订单
    "hard": [
        "req-10-esp32s3-industrial-4layer.md",
        "req-11-isolated-dido-module.md",
        "req-12-hybrid-dual-mcu-gateway.md",
        "req-13-env-sensor-motherboard.md",
        "req-14-door-sensor-e2e.md",
    ],
    "smoke": [
        "req-01-esp32s3-mini-2layer.md",
        "req-08-liion-protection-freeform.md",
        "req-11-isolated-dido-module.md",
    ],
}
# daily = 简单层全部 + 中等抽 3(电源树/自由拓扑/refine 歧义) + 困难抽 1(隔离历史难例) = 8 个
_TIER["daily"] = _TIER["easy"] + [
    "req-05-battery-ble-telemetry.md",
    "req-08-liion-protection-freeform.md",
    "req-09-ambiguous-sensor-node.md",
    "req-11-isolated-dido-module.md",
]


def _pick(tier: str | None) -> list[str]:
    if tier in (None, "all"):
        return _REQS
    if tier == "rest":
        # 增量层:全量减去 smoke/daily 已覆盖的(发版时先跑 daily 再跑 rest,零重复)
        covered = set(_TIER["daily"])
        return [r for r in _REQS if r not in covered and (_REQ_DIR / r).exists()]
    if tier not in _TIER:
        raise ValueError(f"未知回归级 '{tier}',可选: easy/medium/hard(难度层) smoke/daily/rest(回归级) all(全量重跑)")
    return [r for r in _TIER[tier] if (_REQ_DIR / r).exists()]


def _health_check() -> None:
    """需求间健康检查:连接器假死时显式中断(断点已存,重启 EasyEDA 后续跑),
    而不是每个需求空转重试浪费数十分钟。"""
    import time

    from dotenv import load_dotenv

    load_dotenv()
    from edaloop.generate.adapter import AdapterError, EasyedaAdapter

    adapter = EasyedaAdapter()
    for attempt in range(3):
        rc, _, _ = adapter.run(["sch", "pages"])
        if rc == 0:
            return
        time.sleep(8)
    raise RuntimeError(
        "连接器无响应(EasyEDA 可能假死):请重启 EasyEDA Pro 并打开工程,然后重跑同一命令——断点已保存,将从剩余需求继续"
    )


def _clear_page() -> None:
    import time

    from dotenv import load_dotenv

    load_dotenv()
    from edaloop.generate.adapter import EasyedaAdapter

    adapter = EasyedaAdapter()
    last_rc = -1
    for i in range(4):
        rc, out, _ = adapter.run(["sch", "pages"])
        if rc == 0:
            rc, out, _ = adapter.run(["sch", "clear"])
            if rc == 0:
                return
        last_rc = rc
        time.sleep(6)
    raise RuntimeError(f"sch clear 失败(warmup 后 rc={last_rc})")


_STATE = Path("runs/w3-loop-state.json")


def _load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"rows": {}}


def _save_state(state: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def run_w3_loop_eval(max_rounds: int = 5, dry_run: bool = False, resume: bool = True, tier: str | None = None) -> dict:
    reqs = _pick(tier)
    if tier not in (None, "all"):
        state_path = Path(f"runs/w3-loop-state-{tier}.json")
    else:
        state_path = Path("runs/w3-loop-state.json")
    state = _load_state(state_path) if resume else {"rows": {}}
    for name in reqs:
        # P5-0: 只跳 PASS;HALT/ERROR 重跑——resume 曾把环境崩溃遗留的 HALT 行当
        # 已完成跳过,smoke 变 2/3 而不自知(2026-08-23 实证)。HALT=同错 2 轮需
        # 人工介入,操作者重发 eval 即代表要新尝试,旧行已留在上一 run 的 audit 里。
        if state["rows"].get(name, {}).get("status") == "PASS":
            print(f"skip(done) {name}: {state['rows'][name]}", flush=True)
            continue
        md = (_REQ_DIR / name).read_text(encoding="utf-8")
        body = md.split("## 期望指标")[0]
        if not dry_run:
            _health_check()
            _clear_page()
        try:
            ir, result = stage_run(body, source=name, max_rounds=max_rounds, dry_run=dry_run)
            row = {
                "req": name,
                "status": result.status,
                "rounds": result.converged_round,
                "n_rounds": len(result.rounds),
            }
        except Exception as e:
            import traceback

            Path("runs/w3-last-error.txt").write_text(
                f"{name}\n{traceback.format_exc()}", encoding="utf-8"
            )
            row = {"req": name, "status": f"ERROR:{type(e).__name__}", "rounds": None, "n_rounds": 0}
        state["rows"][name] = row
        _save_state(state, state_path)
        print(row, flush=True)
    rows = list(state["rows"].values())
    n = len(rows)
    pass3 = sum(1 for r in rows if r["status"] == "PASS" and r["rounds"] and r["rounds"] <= 3)
    pass5 = sum(1 for r in rows if r["status"] == "PASS" and r["rounds"] and r["rounds"] <= 5)
    summary = {
        "rows": rows,
        "pass@3": pass3 / n if n else 0,
        "pass@5": pass5 / n if n else 0,
        "go3": pass3 / n >= 0.6 if n else False,
        "go5": pass5 / n >= 0.8 if n else False,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    Path("runs/w3-loop-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return summary
