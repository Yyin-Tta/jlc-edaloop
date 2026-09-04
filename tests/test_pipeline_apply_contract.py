import json
from pathlib import Path

from edaloop.generate import pipeline
from edaloop.generate.audit import AuditLog
from edaloop.generate.models import Action, BlockPlan


class _ApplyAdapter:
    """Small command fake for the low-level pipeline contract tests."""

    def __init__(self, gate: dict | None = None) -> None:
        self.gate = gate or {"verdict": "pass", "stages": []}
        self.calls: list[list[str]] = []

    def check_version(self) -> str:
        return "1.2.10"

    def daemon_health(self) -> dict:
        return {"status": "found"}

    def run_json(self, args: list[str]) -> dict:
        self.calls.append(list(args))
        if args[:2] == ["sch", "gate"]:
            return dict(self.gate)
        return {"ok": "applied"}

    def delete_primitives(self, ids: list[str]) -> dict:
        self.calls.append(["sch", "prim-delete", *ids])
        return {"ok": True}


class _RcGateAdapter(_ApplyAdapter):
    def run_json_with_rc(self, args: list[str]):
        self.calls.append(list(args))
        if args[:2] == ["sch", "gate"]:
            return 7, dict(self.gate), "checker failed"
        return 0, {"ok": "applied"}, ""


def _run_stage_apply(monkeypatch, tmp_path: Path, adapter: _ApplyAdapter):
    plan = BlockPlan(id="plan-contract", source="fixture.md")
    actions = [
        Action(
            kind="sch-gate",
            args=["sch", "gate", "--json"],
            desc="gate",
        )
    ]
    monkeypatch.setattr(pipeline, "compile_actions", lambda _plan, _catalog: actions)
    audit = AuditLog(tmp_path / "audit")
    return pipeline.stage_apply(
        plan,
        catalog={"fixture": object()},
        adapter=adapter,
        audit=audit,
    )


def test_stage_apply_downgrades_nominal_pass_to_unverified(monkeypatch, tmp_path: Path) -> None:
    adapter = _ApplyAdapter({"verdict": "PASS", "stages": []})

    summary = _run_stage_apply(monkeypatch, tmp_path, adapter)

    assert summary["raw_gate_verdict"] == "PASS"
    assert summary["verification"]["normalized_verdict"] == "pass"
    assert summary["gate_verdict"] == "unverified"
    assert summary["mode"] == "low-level-experimental"
    assert summary["verified"] is False
    assert summary["verification"]["verified"] is False
    gate = next(r for r in summary["results"] if r["kind"] == "gate")
    assert gate["raw_verdict"] == "PASS"
    assert gate["verdict"] == "unverified"
    assert "--strict" in gate["args"]
    assert any(c[:2] == ["sch", "gate"] and "--strict" in c for c in adapter.calls)
    events = [
        json.loads(line)
        for line in (tmp_path / "audit" / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    contract = next(e for e in events if e["kind"] == "stage-apply-contract")
    assert contract["raw_gate_verdict"] == "PASS"
    assert contract["gate_verdict"] == "unverified"
    assert contract["verified"] is False


def test_stage_apply_preserves_real_gate_failure_but_never_marks_verified(
    monkeypatch, tmp_path: Path
) -> None:
    summary = _run_stage_apply(
        monkeypatch,
        tmp_path,
        _ApplyAdapter({"verdict": "fail", "stages": []}),
    )

    assert summary["raw_gate_verdict"] == "fail"
    assert summary["gate_verdict"] == "fail"
    assert summary["verification"]["verified"] is False


def test_stage_apply_downgrades_pass_with_nonzero_gate_rc(monkeypatch, tmp_path: Path) -> None:
    summary = _run_stage_apply(
        monkeypatch,
        tmp_path,
        _RcGateAdapter({"verdict": "pass", "stages": []}),
    )

    assert summary["raw_gate_verdict"] == "pass"
    assert summary["gate_verdict"] == "unknown"
    gate = next(r for r in summary["results"] if r["kind"] == "gate")
    assert gate["rc"] == 7
    assert gate["stderr"] == "checker failed"


def test_stage_apply_without_gate_is_not_run_and_not_verified(monkeypatch, tmp_path: Path) -> None:
    plan = BlockPlan(id="plan-no-gate", source="fixture.md")
    actions = [
        Action(kind="block-apply", block_instance="b1", args=["sch", "block-apply", "b1"])
    ]
    monkeypatch.setattr(pipeline, "compile_actions", lambda _plan, _catalog: actions)
    summary = pipeline.stage_apply(
        plan,
        catalog={"fixture": object()},
        adapter=_ApplyAdapter(),
        audit=AuditLog(tmp_path / "audit"),
    )

    assert summary["raw_gate_verdict"] == "not-run"
    assert summary["gate_verdict"] == "not-run"
    assert summary["verification"]["verified"] is False


def test_stage_apply_does_not_replay_non_idempotent_partial_block(monkeypatch, tmp_path: Path) -> None:
    plan = BlockPlan(id="plan-partial", source="fixture.md")
    actions = [
        Action(
            kind="block-apply",
            block_instance="b1",
            args=["sch", "block-apply", "b1"],
        )
    ]
    monkeypatch.setattr(pipeline, "compile_actions", lambda _plan, _catalog: actions)

    class _PartialAdapter(_ApplyAdapter):
        def run_json(self, args: list[str]) -> dict:
            self.calls.append(list(args))
            return {
                "status": "failed-partial",
                "rollback": {"survivedPrimitiveIds": ["p1"]},
            }

    adapter = _PartialAdapter()
    summary = pipeline.stage_apply(
        plan,
        catalog={"fixture": object()},
        adapter=adapter,
        audit=AuditLog(tmp_path / "audit"),
    )

    assert len([c for c in adapter.calls if c[:2] == ["sch", "block-apply"]]) == 1
    assert ["sch", "prim-delete", "p1"] in adapter.calls
    assert summary["apply_failures"]


def test_stage_apply_counts_boolean_negative_manifest_as_failure(monkeypatch, tmp_path: Path) -> None:
    plan = BlockPlan(id="plan-negative", source="fixture.md")
    actions = [
        Action(
            kind="block-apply",
            block_instance="b1",
            args=["sch", "block-apply", "b1"],
        )
    ]
    monkeypatch.setattr(pipeline, "compile_actions", lambda _plan, _catalog: actions)

    class _NegativeAdapter(_ApplyAdapter):
        def run_json(self, args: list[str]) -> dict:
            self.calls.append(list(args))
            return {"ok": False}

    summary = pipeline.stage_apply(
        plan,
        catalog={"fixture": object()},
        adapter=_NegativeAdapter(),
        audit=AuditLog(tmp_path / "audit"),
    )

    assert len(summary["apply_failures"]) == 1


def test_stage_apply_marks_negative_or_malformed_manifest_as_failure(
    monkeypatch, tmp_path: Path
) -> None:
    plan = BlockPlan(id="plan-manifest", source="fixture.md")
    actions = [
        Action(kind="block-apply", block_instance="bad", args=["sch", "block-apply", "bad"]),
        Action(kind="block-apply", block_instance="shape", args=["sch", "block-apply", "shape"]),
    ]
    monkeypatch.setattr(pipeline, "compile_actions", lambda _plan, _catalog: actions)

    class _ManifestAdapter(_ApplyAdapter):
        def run_json(self, args: list[str]):
            self.calls.append(list(args))
            if args[-1] == "bad":
                return {"ok": True, "status": "failed"}
            return ["not", "an", "object"]

    summary = pipeline.stage_apply(
        plan,
        catalog={"fixture": object()},
        adapter=_ManifestAdapter(),
        audit=AuditLog(tmp_path / "audit"),
    )

    assert {item["instance"] for item in summary["apply_failures"]} == {"bad", "shape"}
