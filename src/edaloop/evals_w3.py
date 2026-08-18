from __future__ import annotations

import json
from pathlib import Path

from edaloop.generate.pipeline import stage_run

_REQ_DIR = Path("evals/requirements")
_REQS = sorted(p.name for p in _REQ_DIR.glob("req-*.md"))


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


def _load_state() -> dict:
    if _STATE.exists():
        return json.loads(_STATE.read_text(encoding="utf-8"))
    return {"rows": {}}


def _save_state(state: dict) -> None:
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    _STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def run_w3_loop_eval(max_rounds: int = 5, dry_run: bool = False, resume: bool = True) -> dict:
    state = _load_state() if resume else {"rows": {}}
    for name in _REQS:
        if name in state["rows"]:
            print(f"skip(done) {name}: {state['rows'][name]}", flush=True)
            continue
        md = (_REQ_DIR / name).read_text(encoding="utf-8")
        body = md.split("## 期望指标")[0]
        if not dry_run:
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
        _save_state(state)
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
