"""UI 会话层纯逻辑测试:audit listener 挂点 + 目录约定 + 事件翻译。

不依赖 chainlit、不碰 LLM/EasyEDA(app.py 的展示层逻辑靠人工冒烟)。
"""

from __future__ import annotations

import json
from pathlib import Path

from edaloop.generate.audit import AuditLog
from edaloop.ui.session import format_event, save_attachment, session_dir


class TestAuditListener:
    def test_listener_receives_events(self, tmp_path):
        seen: list[tuple[str, dict]] = []
        audit = AuditLog(tmp_path / "run", listener=lambda k, f: seen.append((k, f)))
        audit.event("round-plan", round_no=1, blocks=["U1", "R1"], uncovered=[])
        assert seen == [("round-plan", {"round_no": 1, "blocks": ["U1", "R1"], "uncovered": []})]
        rec = json.loads((tmp_path / "run" / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert rec["kind"] == "round-plan"  # 落盘不受 listener 影响

    def test_listener_crash_does_not_break_run(self, tmp_path):
        def boom(kind: str, fields: dict) -> None:
            raise RuntimeError("ui down")

        audit = AuditLog(tmp_path / "run", listener=boom)
        audit.event("loop-done", status="PASS", rounds=1)  # 不得抛
        assert (tmp_path / "run" / "audit.jsonl").exists()

    def test_no_listener_by_default(self, tmp_path):
        audit = AuditLog(tmp_path / "run")
        audit.event("ir", source="req.md", revision=1)  # 既有用法不受影响


class TestFormatEvent:
    def test_round_plan(self):
        line = format_event("round-plan", {"round_no": 2, "blocks": ["U1"] * 3, "uncovered": ["x"]})
        assert "轮 2" in line and "3 blocks" in line and "uncovered 1" in line

    def test_round_validate(self):
        line = format_event(
            "round-validate", {"round_no": 1, "gate": "pass", "blocking": [], "weak": ["w"]}
        )
        assert "gate=pass" in line and "blocking=0" in line and "weak=1" in line

    def test_loop_done_and_halt(self):
        assert "PASS" in format_event("loop-done", {"status": "PASS", "rounds": 3})
        assert "HALT" in format_event("loop-halt", {"reason": "同错 2 轮"})

    def test_place_ok_and_fail(self):
        assert "U1" in format_event("sch-place", {"instance": "U1", "page": "P1", "ok": True})
        line = format_event(
            "block-apply", {"instance": "U2", "page": "P2", "status": "failed-partial"}
        )
        assert "✗" in line and "failed-partial" in line and "U2" in line

    def test_gate_pass_is_quiet(self):
        assert format_event("gate", {"page": "P1", "verdict": "pass"}) is None
        assert "blocked" in format_event("gate", {"page": "P2", "verdict": "blocked"})

    def test_delivery(self):
        line = format_event("delivery", {"artifacts": {"svg": "a.svg", "bom": "b.json"}})
        assert "svg" in line and "bom" in line
        assert format_event("delivery", {"artifacts": {}}) is None

    def test_quiet_events_return_none(self):
        assert format_event("lib-search", {"instance": "U1"}) is None
        assert format_event("pin-verify", {"ok": True}) is None


class TestSessionDirs:
    def test_session_dir_layout(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        d = session_dir("abc-123")
        assert d == Path("runs/ui") / "abc-123"
        assert (d / "attachments").is_dir()

    def test_session_id_sanitized(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert session_dir("a/b c").name == "a_b_c"

    def test_save_attachment_no_traversal(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        p = save_attachment("s1", "../../x.md", b"data")
        assert p.parent == session_dir("s1") / "attachments"
        assert p.suffix == ".md"  # 扩展名保留(路由靠它分需求文件/PDF)
        assert p.read_bytes() == b"data"
        assert ".." != p.name and "/" not in p.name
