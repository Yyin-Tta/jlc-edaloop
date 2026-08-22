from __future__ import annotations

import json
from pathlib import Path

from edaloop.refine import collect_questions, refine_run, retry_query


def _write_audit(tmp_path: Path, events: list[dict]) -> Path:
    d = tmp_path / "run-x"
    d.mkdir(parents=True, exist_ok=True)
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
    uid = next(q["id"] for q in collect_questions(str(d)) if q["source"] == "uncovered")
    result = refine_run(str(d), {"Q1": "升压", uid: "补充细节"})
    assert result["applied"] == 1
    assert result["ir_revision"] == 2
    assert (d / "ir-v2.json").exists()
    assert (d / "refine-meta.json").exists()
    assert result["retry_queries"], "uncovered 补充应产出二次检索词"


def test_question_ids_stable_across_list_changes(tmp_path) -> None:
    """P4-5③ 编号单源化:问题清单增删,U-id 不漂移(按位置编号会把答案串到别的问题上)。"""
    d = _write_audit(tmp_path, _EVENTS)
    uid_before = next(q["id"] for q in collect_questions(str(d)) if q["source"] == "uncovered")
    extra = _EVENTS + [{"kind": "critic", "findings": [{"code": "CRITIC_DECOUPLING", "evidence": "去耦缺失"}]}]
    d2 = _write_audit(tmp_path / "v2", extra)
    qs2 = collect_questions(str(d2))
    uid_after = next(q["id"] for q in qs2 if q["source"] == "uncovered")
    assert uid_before == uid_after
    assert any(q["id"].startswith("C-") for q in qs2)


def test_refine_manual_answers_inject_decisions(tmp_path) -> None:
    """P4-5③ 双注入:U 答案进 decisions(planner 摘要可见),不再是只影响检索词。"""
    d = _write_audit(tmp_path, _EVENTS)
    uid = next(q["id"] for q in collect_questions(str(d)) if q["source"] == "uncovered")
    result = refine_run(str(d), {"Q1": "升压", uid: "补充:需要 LED 指示,5V 供电"})
    ir2 = json.loads((d / "ir-v2.json").read_text(encoding="utf-8"))
    assert ir2["decisions"].get(uid) == "补充:需要 LED 指示,5V 供电"
    assert result["manual_applied"] >= 1
    assert result["ir_revision"] >= 2


def test_retry_query_embedding_neighbor(tmp_path) -> None:
    """P4-5③ embedding 近邻:伪 embedder 把「点灯」拉近「LED 指示」块词,查询词随之扩展。"""

    class _Emb:
        dim = 2

        def embed_query(self, text: str) -> list[float]:
            return [1.0, 0.1] if "LED" in text or "灯" in text else [0.1, 1.0]

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return [self.embed_query(t) for t in texts]

    q = retry_query("用户点灯需求", _Emb(), ["LED 指示灯", "RS485 收发器", "LDO 稳压"])
    assert "LED 指示灯" in q
    q2 = retry_query("用户点灯需求", None, None)  # 无 embedder 落回静态表
    assert q2.startswith("用户点灯需求")


def test_retry_query_expands_led() -> None:
    q = retry_query("电源指示 LED 与限流电阻")
    assert "LED" in q or "led" in q
    assert "限流" in q


def test_retry_query_no_hint_passthrough() -> None:
    q = retry_query("CAN 收发器")
    assert "CAN" in q
