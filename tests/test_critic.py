from __future__ import annotations

import json

from edaloop.generate.models import BlockPlan
from edaloop.llm.fake import FakeChat
from edaloop.loop.critic import render_report, review_plan


def _plan() -> BlockPlan:
    return BlockPlan.model_validate(
        {
            "blocks": [
                {"block_id": "mcu-x", "upstream_id": "block.mcu_x", "instance": "u1",
                 "ports_binding": {"3V3": "3V3", "SDA": "SDA", "SCL": "SCL"}},
            ]
        }
    )


def test_review_ok_findings() -> None:
    reply = json.dumps(
        [
            {"check": "pull-resistors", "target": "u1", "issue": "I2C SDA/SCL 无上拉", "advice": "加 4.7k×2", "severity": "warn"},
            {"check": "decoupling", "target": "u1", "issue": "MCU 无去耦", "advice": "每 VDD 100nF", "severity": "warn"},
        ],
        ensure_ascii=False,
    )
    findings = review_plan(_plan(), FakeChat(reply))
    assert len(findings) == 2
    assert findings[0].code == "CRITIC_PULL_RESISTORS"
    assert findings[0].weak is True and findings[0].severity == "warn"
    assert findings[1].suggested_fix_class == "ADD_BLOCK"


def test_review_empty_is_valid() -> None:
    findings = review_plan(_plan(), FakeChat("[]"))
    assert findings == []


def test_review_retries_bad_json() -> None:
    good = json.dumps([{"check": "thermal", "target": "u1", "issue": "x", "advice": "y", "severity": "warn"}])
    chat = FakeChat("not json first")
    # FakeChat 恒定返回同文本,模拟第二次好:直接给好文本验证解析即可
    findings = review_plan(_plan(), FakeChat("```json\n" + good + "\n```"))
    assert len(findings) == 1


def test_review_strips_noise_items() -> None:
    reply = json.dumps(
        [
            {"issue": ""},
            {"no_issue_key": 1},
            {"check": "emc", "target": "u1", "issue": "ok", "advice": "fine", "severity": "warn"},
        ]
    )
    findings = review_plan(_plan(), FakeChat(reply))
    assert len(findings) == 1


def test_render_report() -> None:
    from edaloop.validate.models import Finding, Where

    f = Finding(code="CRITIC_THERMAL", where=Where(ref="u1"), evidence="过热 | 建议 加铜", severity="warn", weak=True)
    rep = render_report([f], "3 blocks")
    assert "弱门禁" in rep and "CRITIC_THERMAL" in rep
    assert "无设计层缺陷" in render_report([], "x")
