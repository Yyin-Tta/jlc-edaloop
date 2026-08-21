from __future__ import annotations

import json
from pathlib import Path

from edaloop.refine import collect_questions, refine_run, retry_query


def _write_audit(tmp_path: Path, events: list[dict]) -> Path:
    d = tmp_path / "run-x"
    d.mkdir(exist_ok=True)
    (d / "audit.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8"
    )
    return d


_EVENTS = [
    {
        "kind": "ir",
        "ir": {
            "source": "test.md",
            "open_questions": [
                {"id": "Q1", "question": "升压还是降压?", "options": ["A", "B"]},
                {"id": "Q2", "question": "端子选型?", "options": []},
            ],
        },
    },
    {"kind": "round-plan", "round_no": 1, "uncovered": ["电源指示 LED 与限流电阻"]},
]


def test_collect_questions(tmp_path) -> None:
    d = _write_audit(tmp_path, _EVENTS)
    qs = collect_questions(str(d))
    sources = [q["source"] for q in qs]
    assert sources.count("open_question") == 2
    assert sources.count("uncovered") == 1
    ids = [q["id"] for q in qs]
    assert "Q1" in ids and any(i.startswith("U") for i in ids)


def test_refine_run_applies_and_writes(tmp_path) -> None:
    d = _write_audit(tmp_path, _EVENTS)
    result = refine_run(str(d), {"Q1": "升压", "U3": "补充细节"})
    assert result["applied"] == 1
    assert result["ir_revision"] == 2
    assert (d / "ir-v2.json").exists()
    assert (d / "refine-meta.json").exists()
    assert result["retry_queries"], "uncovered 补充应产出二次检索词"


def test_retry_query_expands_led() -> None:
    q = retry_query("电源指示 LED 与限流电阻")
    assert "LED" in q or "led" in q
    assert "限流" in q


def test_retry_query_no_hint_passthrough() -> None:
    q = retry_query("CAN 收发器")
    assert "CAN" in q
