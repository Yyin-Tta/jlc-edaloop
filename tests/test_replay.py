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
