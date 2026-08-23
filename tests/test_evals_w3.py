"""P5-0 单测:w3-loop eval resume 只跳 PASS——HALT/ERROR 行必须重跑。

背景:resume 曾把环境崩溃遗留的 HALT 行当已完成跳过,smoke 静默变 2/3(2026-08-23)。
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import edaloop.evals_w3 as w3
from edaloop.evals_w3 import run_w3_loop_eval


def _fake_result(status: str = "PASS", rounds: int = 2):
    return types.SimpleNamespace(status=status, converged_round=rounds, rounds=list(range(rounds)))


def _setup(tmp_path: Path, monkeypatch, state_rows: dict) -> list[str]:
    req_dir = tmp_path / "evals" / "requirements"
    req_dir.mkdir(parents=True)
    for name in ("a.md", "b.md"):
        (req_dir / name).write_text(f"{name} 正文\n## 期望指标\n", encoding="utf-8")
    state_path = tmp_path / "runs" / "w3-loop-state-smoke.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"rows": state_rows}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(w3, "_pick", lambda tier: ["a.md", "b.md"])
    called: list[str] = []
    monkeypatch.setattr(
        w3,
        "stage_run",
        lambda body, source, max_rounds, dry_run: called.append(source) or (None, _fake_result()),
    )
    return called


def test_resume_skips_only_pass(tmp_path: Path, monkeypatch):
    rows = {
        "a.md": {"req": "a.md", "status": "HALT", "rounds": None, "n_rounds": 2},
        "b.md": {"req": "b.md", "status": "PASS", "rounds": 1, "n_rounds": 1},
    }
    called = _setup(tmp_path, monkeypatch, rows)

    summary = run_w3_loop_eval(tier="smoke", dry_run=True, resume=True)

    assert called == ["a.md"]  # HALT 重跑,PASS 跳过
    assert summary["pass@3"] == 1.0
    saved = json.loads((tmp_path / "runs" / "w3-loop-state-smoke.json").read_text(encoding="utf-8"))
    assert saved["rows"]["a.md"]["status"] == "PASS"  # HALT 行被新结果覆盖
    assert saved["rows"]["b.md"]["status"] == "PASS"


def test_resume_reruns_error_rows(tmp_path: Path, monkeypatch):
    rows = {
        "a.md": {"req": "a.md", "status": "ERROR:RuntimeError", "rounds": None, "n_rounds": 0},
    }
    called = _setup(tmp_path, monkeypatch, rows)

    run_w3_loop_eval(tier="smoke", dry_run=True, resume=True)

    assert called == ["a.md", "b.md"]  # ERROR 行重跑;b.md 无历史行,正常执行
