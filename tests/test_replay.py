from __future__ import annotations

import json
from pathlib import Path

import pytest

from edaloop.replay import ReplayError, replay_run


def _write_audit(tmp_path: Path, events: list[dict]) -> Path:
    d = tmp_path / "run-test"
    d.mkdir()
    (d / "audit.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8"
    )
    return d


def test_replay_missing_audit(tmp_path) -> None:
    with pytest.raises(ReplayError):
        replay_run(str(tmp_path / "nope"))


def test_replay_no_actions(tmp_path) -> None:
    d = _write_audit(tmp_path, [{"kind": "ir", "round_no": None}])
    with pytest.raises(ReplayError):
        replay_run(str(d), dry_run=True)


def test_replay_dry_run_picks_final_round(tmp_path) -> None:
    events = [
        {"kind": "ir", "round_no": None},
        {"kind": "page-clear", "round_no": 1},
        {"kind": "block-apply", "round_no": 1, "args": ["sch", "block-apply", "b1"]},
        {"kind": "round-validate", "round_no": 1},
        {"kind": "page-clear", "round_no": 2},
        {"kind": "block-apply", "round_no": 2, "args": ["sch", "block-apply", "b1"]},
        {"kind": "block-apply", "round_no": 2, "args": ["sch", "block-apply", "b2"]},
        {"kind": "gate", "round_no": 2, "args": ["sch", "gate"]},
    ]
    d = _write_audit(tmp_path, events)
    result = replay_run(str(d), dry_run=True)
    assert result["final_round"] == 2
    assert result["replayed"] == 4
    assert result["gate_verdict"] == "not-run"


class _ReplayAdapter:
    def __init__(self) -> None:
        self.ran: list[str] = []

    def run(self, args):
        self.ran.append(" ".join(args))
        return 0, "{}", ""

    def run_json(self, args):
        self.ran.append(" ".join(args))
        if args[1] == "gate":
            return {"verdict": "pass"}
        return {"ok": "applied"}

    def clear_all_pages(self):
        self.ran.append("clear-all")


def test_replay_with_adapter(tmp_path) -> None:
    events = [
        {"kind": "page-clear", "round_no": 1},
        {"kind": "lib-search", "round_no": 1, "args": ["lib", "search", "--query", "C7512"]},
        {"kind": "sch-place", "round_no": 1, "args": ["sch", "place", "--lib", "L", "--uuid", "U"]},
        {"kind": "sch-autoconnect", "round_no": 1, "args": ["sch", "autoconnect", "--pin", "U1:1B"]},
        {"kind": "gate", "round_no": 1, "args": ["sch", "gate", "--json"]},
    ]
    d = _write_audit(tmp_path, events)
    adapter = _ReplayAdapter()
    result = replay_run(str(d), run_json=adapter)
    assert result["gate_verdict"] == "pass"
    assert result["replayed"] == 5
    assert any("block-apply" not in r for r in adapter.ran)
    assert "clear-all" in adapter.ran


def test_replay_full_plus_repair_rounds(tmp_path) -> None:
    """修复轮重放=最后全量轮(r2)动作 + 其后修复轮(r3)脏页动作拼接。

    - r2 全量:page-clear(无 mode,整档清)+ 两块;
    - r3 修复:page-clear(mode=repair, dirty=[P2])+ 一块。
    断言:base_round=2、两轮动作都重放、整档清恰一次、P2 按页清。"""
    events = [
        {"kind": "ir", "round_no": None},
        {"kind": "page-clear", "round_no": 2},
        {"kind": "block-apply", "round_no": 2, "args": ["sch", "block-apply", "b1"],
         "page": "P1", "instance": "u1"},
        {"kind": "block-apply", "round_no": 2, "args": ["sch", "block-apply", "b2"],
         "page": "P2", "instance": "u2"},
        {"kind": "gate", "round_no": 2, "args": ["sch", "gate", "--json"]},
        {"kind": "round-plan", "round_no": 3, "source": "repair-stash"},
        {"kind": "repair-round", "round_no": 3, "dirty": ["P2"], "frozen": ["P1"]},
        {"kind": "page-clear", "round_no": 3, "mode": "repair", "dirty": ["P2"]},
        {"kind": "block-apply", "round_no": 3, "args": ["sch", "block-apply", "b2"],
         "page": "P2", "instance": "u2"},
        {"kind": "gate", "round_no": 3, "args": ["sch", "gate", "--json"]},
    ]
    d = _write_audit(tmp_path, events)
    adapter = _ReplayAdapter()
    result = replay_run(str(d), run_json=adapter)
    assert result["final_round"] == 3 and result["base_round"] == 2
    assert result["replayed"] == 7  # r2:clear+两块+gate=4;r3:clear+一块+gate=3
    assert adapter.ran.count("clear-all") == 1  # 全量轮整档清恰一次
    assert "sch clear --doc P2" in adapter.ran  # 修复轮按页清
    assert adapter.ran.count("sch block-apply b1") == 1  # 冻结页块只来自 r2
    assert adapter.ran.count("sch block-apply b2") == 2  # r2+r3 各一次


def test_replay_repair_only_tail_aborted_degrades_to_full(tmp_path) -> None:
    """末轮是 aborted 修复轮(无可重放动作)→ 拼接退化为最后全量轮,不空转。"""
    events = [
        {"kind": "page-clear", "round_no": 1},
        {"kind": "block-apply", "round_no": 1, "args": ["sch", "block-apply", "b1"]},
        {"kind": "gate", "round_no": 1, "args": ["sch", "gate", "--json"]},
        {"kind": "repair-round", "round_no": 2, "dirty": ["P2"], "frozen": ["P1"]},
        {"kind": "repair-trial-abort", "round_no": 2, "reason": "canvas-uncleared:P2"},
    ]
    d = _write_audit(tmp_path, events)
    result = replay_run(str(d), dry_run=True)
    assert result["base_round"] == 1 and result["final_round"] == 2
    assert result["replayed"] == 3  # r1 的 clear+block-apply+gate
