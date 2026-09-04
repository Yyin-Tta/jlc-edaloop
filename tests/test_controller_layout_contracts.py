from __future__ import annotations

import json
from pathlib import Path

import pytest

from edaloop.generate.audit import AuditLog
from edaloop.generate.adapter import EasyedaAdapter
from edaloop.generate.models import Action, BlockPlan, PlannedBlock
from edaloop.intent.ir import DesignIR
from edaloop.knowledge.models import BlockRecord
from edaloop.llm.fake import FakeChat, FakeEmbedding
from edaloop.loop.controller import (
    LoopController,
    LoopResult,
    _ReadbackPayloadError,
    _validate_terminal_page_context,
)
from edaloop.loop.attribution import attribute
from edaloop.validate.layout import (
    LAYOUT_BODY_OVERLAP,
    LAYOUT_COMPONENT_MISSING,
    LAYOUT_DUPLICATE_MARKER,
    LAYOUT_PIN_COINCIDENCE,
    LAYOUT_PIN_NET_MISMATCH,
    LAYOUT_READ_UNVERIFIED,
    LAYOUT_SNAPSHOT_INVALID,
    audit_layout_snapshot,
)
from edaloop.validate.models import Finding, Where


def _controller(tmp_path: Path, adapter, *, strict: bool = True) -> LoopController:
    ir = DesignIR.model_validate(
        {
            "source": "fixture.md",
            "power": {"rails": [{"name": "VCC", "voltage": 3.3}]},
        }
    )
    return LoopController(
        ir,
        {},
        lambda _query: [],
        FakeChat("[]"),
        adapter,
        AuditLog(tmp_path / "audit"),
        strict_layout=strict,
    )


def _box(x1: float, y1: float, x2: float, y2: float) -> dict[str, float]:
    return {"minX": x1, "minY": y1, "maxX": x2, "maxY": y2}


def _part(ref: str, bbox: tuple[float, float, float, float], *, pins=()) -> dict:
    return {
        "componentType": "part",
        "designator": ref,
        "bbox": _box(*bbox),
        "pins": list(pins),
    }


class _SnapshotAdapter:
    def __init__(self, components: list[dict], clusters: list[dict] | None = None):
        self.components = components
        self.cluster_items = clusters if clusters is not None else [
            {"designator": c["designator"], "body": c["bbox"], "box": c["bbox"]}
            for c in components
            if c.get("componentType") == "part"
        ]

    def run(self, args):
        if args[1] == "list":
            return 0, json.dumps({"result": {"components": self.components}}), ""
        if args[1] == "clusters":
            return 0, json.dumps(
                {
                    "clusters": self.cluster_items,
                    "sheetUsable": _box(30, 30, 1140, 795),
                }
            ), ""
        raise AssertionError(f"unexpected adapter command: {args}")


def test_strict_terminal_audit_rejects_empty_list_readback(tmp_path: Path) -> None:
    controller = _controller(tmp_path, _SnapshotAdapter([]))
    actions = [Action(kind="block-apply", block_instance="i1", page="P1")]

    findings = controller._terminal_layout_audit(
        actions, 1, {"P1": {"i1": ["U1"]}}
    )

    assert any(f.code == LAYOUT_READ_UNVERIFIED for f in findings)
    assert controller._layout_snapshots["P1"].readback_status == "error"


def test_terminal_page_context_accepts_matching_document_uuid() -> None:
    payload = {
        "context": {
            "documentUuid": "uuid-p1",
            "documentType": "schematic",
            "pageName": "P1",
        }
    }

    assert _validate_terminal_page_context(
        payload, page="P1", expected_uuid="uuid-p1", kind="sch list"
    ) == "uuid-p1"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"context": {}},
        {"context": {"documentUuid": "uuid-p2"}},
    ],
)
def test_terminal_page_context_rejects_missing_or_wrong_identity(payload: dict) -> None:
    with pytest.raises(_ReadbackPayloadError):
        _validate_terminal_page_context(
            payload, page="P1", expected_uuid="uuid-p1", kind="sch list"
        )


def _strict_context_adapter(context: dict | None) -> EasyedaAdapter:
    """Real-adapter-shaped fake used only for the strict identity contract."""

    def runner(args: list[str]):
        if args[:2] == ["sch", "pages"]:
            return 0, json.dumps({"result": {"pages": [{"name": "P1", "uuid": "uuid-p1"}]}}), ""
        if args[:2] == ["sch", "list"]:
            payload = {
                "ok": True,
                "result": {"components": [_part("U1", (100, 300, 160, 360))]},
            }
            if context is not None:
                payload["context"] = context
            return 0, json.dumps(payload), ""
        if args[:3] == ["sch", "clusters", "--json"]:
            return 0, json.dumps(
                {
                    "clusters": [{"designator": "U1", "body": _box(100, 300, 160, 360)}],
                    "sheetUsable": _box(30, 30, 1140, 795),
                }
            ), ""
        raise AssertionError(f"unexpected adapter command: {args}")

    adapter = EasyedaAdapter(runner=runner)
    # Avoid the adapter's normal daemon-window discovery; this fake only
    # exercises the terminal page-identity contract.
    adapter._window_resolved = True
    return adapter


@pytest.mark.parametrize(
    ("context", "verified"),
    [
        ({"documentUuid": "uuid-p1", "documentType": "schematic"}, True),
        (None, False),
        ({"documentUuid": "uuid-p2", "documentType": "schematic"}, False),
    ],
)
def test_strict_real_adapter_page_identity_is_fail_closed(
    tmp_path: Path, context: dict | None, verified: bool
) -> None:
    controller = _controller(tmp_path, _strict_context_adapter(context))

    snapshot, findings = controller._read_layout_snapshot("P1", 1, [], ["U1"])

    assert snapshot.verified_readback is verified
    if verified:
        assert not any(f.code == LAYOUT_SNAPSHOT_INVALID for f in findings)
    else:
        assert any(f.code == LAYOUT_SNAPSHOT_INVALID for f in findings)
        assert snapshot.validation_errors


def test_terminal_page_uuid_does_not_trust_failed_inventory_envelope(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(args: list[str]):
        calls.append(list(args))
        if args[:2] == ["sch", "pages"]:
            # A stale nested page table beside ok:false is not an identity
            # anchor; the resolver must fall through to doc ls.
            return 0, json.dumps({
                "ok": False,
                "result": {"pages": [{"name": "P1", "uuid": "stale"}]},
            }), ""
        if args[:2] == ["doc", "ls"]:
            return 0, json.dumps({
                "result": {
                    "documents": [
                        {"name": "P1", "uuid": "live", "type": "schematic"}
                    ]
                }
            }), ""
        raise AssertionError(args)

    adapter = EasyedaAdapter(runner=runner)
    adapter._window_resolved = True
    controller = _controller(tmp_path, adapter)

    assert controller._terminal_page_uuid("P1") == "live"
    assert calls[0][:2] == ["sch", "pages"]
    assert calls[1][:3] == ["doc", "ls", "--json"]


def test_strict_terminal_audit_rejects_nonzero_list_even_with_json_stdout(tmp_path: Path) -> None:
    class NonzeroListAdapter(_SnapshotAdapter):
        def run(self, args):
            if args[1] == "list":
                return 1, json.dumps({"result": {"components": self.components}}), "connector warning"
            return super().run(args)

    controller = _controller(tmp_path, NonzeroListAdapter([_part("U1", (100, 300, 160, 360))]))

    _snapshot, findings = controller._read_layout_snapshot("P1", 1, [], ["U1"])

    assert any(f.code == LAYOUT_READ_UNVERIFIED for f in findings)
    assert controller._layout_snapshots.get("P1") is None


def test_strict_gate_adds_strict_flag(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    class GateAdapter(_SnapshotAdapter):
        def run(self, args):
            if args[1] == "gate":
                calls.append(list(args))
                return 0, json.dumps(
                    {
                        "verdict": "pass",
                        "stages": [
                            {"name": "layout-lint", "status": "pass"},
                        ],
                    }
                ), ""
            return super().run(args)

        def run_json(self, args):
            _rc, out, _err = self.run(args)
            return json.loads(out)

    controller = _controller(tmp_path, GateAdapter([]))
    actions = [Action(kind="block-apply", block_instance="i1", page="P1")]

    controller._gate_all_pages(["sch", "gate", "--json"], actions, 1)

    assert calls == [["sch", "gate", "--json", "--strict", "--doc", "P1"]]


def test_terminal_audit_checks_expected_designators(tmp_path: Path) -> None:
    components = [_part("U1", (100, 300, 160, 360))]
    controller = _controller(tmp_path, _SnapshotAdapter(components))
    actions = [Action(kind="block-apply", block_instance="i1", page="P1")]

    findings = controller._terminal_layout_audit(
        actions, 1, {"P1": {"i1": ["U1", "C1"]}}
    )

    missing = [f for f in findings if f.code == LAYOUT_COMPONENT_MISSING]
    assert len(missing) == 1
    assert missing[0].where.ref == "C1"


@pytest.mark.parametrize("component_type", ["COMPONENT", "", "DEVICE"])
def test_terminal_snapshot_normalizes_additional_component_type_aliases(
    tmp_path: Path, component_type: str
) -> None:
    raw = {
        "componentType": component_type,
        "designator": "U1",
        "bbox": _box(100, 300, 160, 360),
        "pins": [],
    }
    controller = _controller(tmp_path, _SnapshotAdapter([raw]))

    snapshot, findings = controller._read_layout_snapshot("P1", 1, [], ["U1"])

    assert [c.ref for c in snapshot.body_components] == ["U1"]
    assert snapshot.components[0].component_type == "part"
    assert not any(f.code == LAYOUT_COMPONENT_MISSING for f in findings)


def test_terminal_snapshot_malformed_pin_payload_fails_closed(tmp_path: Path) -> None:
    components = [
        _part("U1", (100, 300, 160, 360), pins=["not-a-pin"]),
        {
            "componentType": "netport",
            "net": "VCC",
            "x": 170,
            "y": 320,
        },
    ]
    controller = _controller(tmp_path, _SnapshotAdapter(components))

    snapshot, findings = controller._read_layout_snapshot("P1", 1, [], ["U1"])

    assert not snapshot.verified_readback
    assert any(f.code == LAYOUT_SNAPSHOT_INVALID for f in findings)


def test_terminal_snapshot_non_object_record_fails_closed(tmp_path: Path) -> None:
    # A scalar mixed into the connector's component array must not be silently
    # filtered out and promoted to a verified partial readback.
    components = [
        _part("U1", (100, 300, 160, 360)),
        {"componentType": "netport", "net": "VCC", "ownerRef": "U1", "pin": "1", "x": 180, "y": 320},
        "malformed-record",
    ]
    controller = _controller(tmp_path, _SnapshotAdapter(components, clusters=[]))

    snapshot, findings = controller._read_layout_snapshot("P1", 1, [], ["U1"])

    assert not snapshot.verified_readback
    assert any(f.code == LAYOUT_SNAPSHOT_INVALID for f in findings)
    assert any("components[2]:expected object record" in error for error in snapshot.validation_errors)


def test_terminal_snapshot_unknown_component_type_fails_closed(tmp_path: Path) -> None:
    # Unknown connector records must not be silently discarded by the
    # controller's part/marker filtering stage.  They are unsupported
    # evidence until a schema mapping is added explicitly.
    components = [
        _part("U1", (100, 300, 160, 360)),
        {
            "componentType": "future-primitive",
            "designator": "X1",
            "bbox": _box(200, 300, 260, 360),
            "pins": [],
        },
    ]
    controller = _controller(tmp_path, _SnapshotAdapter(components))

    snapshot, findings = controller._read_layout_snapshot("P1", 1, [], ["U1"])

    assert snapshot.verified_readback is False
    assert any(f.code == LAYOUT_SNAPSHOT_INVALID for f in findings)
    assert any("unknown component type" in error for error in snapshot.validation_errors)


@pytest.mark.parametrize(
    "component_type",
    ["net-port", "net_flag", "NET-LABEL", "marker"],
)
def test_terminal_snapshot_normalizes_marker_type_aliases(
    tmp_path: Path, component_type: str
) -> None:
    components = [
        _part("U1", (100, 300, 160, 360)),
        {
            "componentType": component_type,
            "net": "VCC",
            "ownerRef": "U1",
            "pin": "1",
            "x": 180,
            "y": 320,
        },
    ]
    controller = _controller(tmp_path, _SnapshotAdapter(components))

    snapshot, _findings = controller._read_layout_snapshot("P1", 1, [], ["U1"])

    assert len(snapshot.markers) == 1
    assert snapshot.markers[0].kind in {"netport", "netflag", "netlabel", "marker"}


@pytest.mark.parametrize(
    ("components", "clusters", "code"),
    [
        (
            [
                _part("U1", (100, 300, 180, 380)),
                _part("U2", (160, 340, 240, 420)),
            ],
            None,
            LAYOUT_BODY_OVERLAP,
        ),
        (
            [
                _part("U1", (100, 300, 140, 340), pins=[{"pinNumber": "1", "x": 120, "y": 320, "net": "A"}]),
                _part("U2", (200, 300, 240, 340), pins=[{"pinNumber": "1", "x": 120, "y": 320, "net": "B"}]),
            ],
            None,
            LAYOUT_PIN_COINCIDENCE,
        ),
        (
            [
                _part("U1", (100, 300, 140, 340)),
                {"componentType": "netport", "net": "VCC", "ownerRef": "U1", "pin": "1", "x": 120, "y": 320},
                {"componentType": "netport", "net": "VCC", "ownerRef": "U1", "pin": "1", "x": 125, "y": 320},
            ],
            None,
            LAYOUT_DUPLICATE_MARKER,
        ),
    ],
)
def test_terminal_audit_reports_layout_findings(tmp_path: Path, components, clusters, code: str) -> None:
    controller = _controller(tmp_path, _SnapshotAdapter(components, clusters))
    snapshot, findings = controller._read_layout_snapshot("P1", 1, [], [c.get("designator") for c in components if c.get("componentType") == "part"])

    assert snapshot.verified_readback
    assert any(f.code == code for f in findings)


def test_terminal_audit_reports_pin_net_mismatch(tmp_path: Path) -> None:
    components = [_part("U1", (100, 300, 140, 340), pins=[{"pinNumber": "1", "x": 120, "y": 320, "net": "GND"}])]
    controller = _controller(tmp_path, _SnapshotAdapter(components))
    actions = [
        Action(
            kind="sch-autoconnect",
            page="P1",
            args=["sch", "autoconnect", "--pin", "U1:1", "--net", "VCC"],
        )
    ]

    _snapshot, findings = controller._read_layout_snapshot("P1", 1, actions, ["U1"])

    assert any(f.code == LAYOUT_PIN_NET_MISMATCH for f in findings)


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("component", "part"),
        ("symbol", "part"),
        ("device", "part"),
        ("net-port", "netport"),
        ("net_flag", "netflag"),
        ("net-label", "netlabel"),
    ],
)
def test_terminal_snapshot_normalizes_component_type_aliases(
    tmp_path: Path, alias: str, canonical: str
) -> None:
    body = {
        "componentType": "component",
        "designator": "U1",
        "bbox": _box(100, 300, 140, 340),
        "pins": [{"pinNumber": "1", "x": 120, "y": 320, "net": "VCC"}],
    }
    if canonical == "part":
        body["componentType"] = alias
        components = [body]
        expected = ["U1"]
    else:
        marker = {
            "componentType": alias,
            "net": "VCC",
            "ownerRef": "U1",
            "pin": "1",
            "x": 120,
            "y": 320,
        }
        components = [body, marker]
        expected = ["U1"]

    controller = _controller(tmp_path, _SnapshotAdapter(components))
    snapshot, findings = controller._read_layout_snapshot(
        "P1", 1, [], expected
    )

    assert snapshot.verified_readback
    if canonical == "part":
        assert [component.component_type for component in snapshot.components] == ["part"]
        assert [component.ref for component in snapshot.components] == ["U1"]
    else:
        assert any(marker.kind == canonical for marker in snapshot.markers)
        marker = next(marker for marker in snapshot.markers if marker.kind == canonical)
        assert marker.owner_ref == "U1"
        assert marker.pin == "1"
    assert not any(f.code == LAYOUT_COMPONENT_MISSING for f in findings)


def test_terminal_audit_resolves_designator_renames_per_instance(tmp_path: Path) -> None:
    components = [
        _part("C2", (100, 300, 140, 340), pins=[
            {"pinNumber": "1", "x": 120, "y": 320, "net": "N_A"},
        ]),
        _part("C3", (300, 300, 340, 340), pins=[
            {"pinNumber": "1", "x": 320, "y": 320, "net": "N_B"},
        ]),
    ]
    controller = _controller(tmp_path, _SnapshotAdapter(components))
    # Both actions retain the template spelling C1.  The actual references
    # differ and must be resolved through the owning block instance.
    controller._designator_map_by_instance = {
        "cap_a": ("C1", "C2"),
        "cap_b": ("C1", "C3"),
    }
    actions = [
        Action(
            kind="sch-autoconnect", block_instance="cap_a", page="P1",
            args=["sch", "autoconnect", "--pin", "C1:1", "--net", "N_A"],
        ),
        Action(
            kind="sch-autoconnect", block_instance="cap_b", page="P1",
            args=["sch", "autoconnect", "--pin", "C1:1", "--net", "N_B"],
        ),
    ]

    snapshot, findings = controller._read_layout_snapshot(
        "P1", 1, actions, ["C2", "C3"]
    )

    assert snapshot.expected_pin_to_net == {"C2:1": "N_A", "C3:1": "N_B"}
    assert not any(f.code == LAYOUT_PIN_NET_MISMATCH for f in findings)


def test_gate_pass_without_stage_evidence_is_unverified(tmp_path: Path) -> None:
    controller = _controller(tmp_path, _SnapshotAdapter([]))
    actions = [Action(kind="block-apply", block_instance="i1", page="P1")]

    findings = controller._check_gate_contract(
        {"verdict": "pass", "stages": []}, actions, 1
    )

    assert len(findings) == 1
    assert findings[0].code == "GATE_UNVERIFIED"


def test_gate_pass_must_cover_every_page(tmp_path: Path) -> None:
    controller = _controller(tmp_path, _SnapshotAdapter([]))
    actions = [
        Action(kind="block-apply", block_instance="i1", page="P1"),
        Action(kind="block-apply", block_instance="i2", page="P2"),
    ]

    findings = controller._check_gate_contract(
        {"verdict": "pass", "stages": [{"stage": "layout", "verdict": "pass", "page": "P1"}]},
        actions,
        1,
    )

    assert len(findings) == 1
    assert "P2" in findings[0].evidence


def test_gate_pass_stage_requires_explicit_success_status(tmp_path: Path) -> None:
    controller = _controller(tmp_path, _SnapshotAdapter([]))
    actions = [Action(kind="block-apply", block_instance="i1", page="P1")]

    findings = controller._check_gate_contract(
        {"verdict": "pass", "stages": [{"stage": "layout", "page": "P1"}]},
        actions,
        1,
    )

    assert len(findings) == 1
    assert findings[0].code == "GATE_UNVERIFIED"
    assert "pass/skipped" in findings[0].evidence


def test_gate_malformed_stage_shape_is_unverified(tmp_path: Path) -> None:
    controller = _controller(tmp_path, _SnapshotAdapter([]))
    actions = [Action(kind="block-apply", block_instance="i1", page="P1")]

    # Feed the merged-report contract directly.  _gate_all_pages also records
    # this key when it sees malformed connector stage entries.
    findings = controller._check_gate_contract(
        {
            "verdict": "pass",
            "stages": [{"stage": "layout", "page": "P1"}],
            "contract_errors": ["P1: gate stages contains non-object entries"],
        },
        actions,
        1,
    )

    assert len(findings) == 1
    assert findings[0].code == "GATE_UNVERIFIED"


def test_gate_all_pages_normalizes_and_fail_closes_malformed_response(tmp_path: Path) -> None:
    class GateAdapter(_SnapshotAdapter):
        def run(self, args):
            if args[1] == "gate":
                return 0, json.dumps({"verdict": "PASS", "stages": ["bad"]}), ""
            return super().run(args)

        def run_json(self, args):
            _rc, out, _err = self.run(args)
            return json.loads(out)

    controller = _controller(tmp_path, GateAdapter([]))
    actions = [Action(kind="block-apply", block_instance="i1", page="P1")]

    report = controller._gate_all_pages(["sch", "gate"], actions, 1)

    assert report["verdict"] == "unknown"
    assert report["contract_errors"]
    assert controller._check_gate_contract(report, actions, 1)[0].code == "GATE_UNVERIFIED"


def _gate_stage_report(*names: str, page: str = "P1", status: str = "pass") -> list[dict]:
    return [{"name": name, "status": status, "page": page} for name in names]


def test_gate_required_stages_default_to_all_five(tmp_path: Path) -> None:
    controller = _controller(tmp_path, _SnapshotAdapter([]))
    actions = [Action(kind="block-apply", block_instance="i1", page="P1")]

    report = {
        "verdict": "pass",
        "stages": _gate_stage_report(
            "layout-lint", "clusters", "check", "bridge-check", "drc"
        ),
    }

    assert controller._gate_required_stages(["sch", "gate", "--json"]) == (
        "layout-lint",
        "clusters",
        "check",
        "bridge-check",
        "drc",
    )
    assert controller._check_gate_contract(report, actions, 1) == []


def test_gate_pass_missing_default_stage_is_unverified(tmp_path: Path) -> None:
    controller = _controller(tmp_path, _SnapshotAdapter([]))
    actions = [Action(kind="block-apply", block_instance="i1", page="P1")]
    report = {
        "verdict": "pass",
        "stages": _gate_stage_report("layout-lint", "clusters", "check", "bridge-check"),
    }

    findings = controller._check_gate_contract(report, actions, 1)

    assert len(findings) == 1
    assert findings[0].code == "GATE_UNVERIFIED"
    assert "drc" in findings[0].evidence


def test_gate_only_derives_selected_required_stages(tmp_path: Path) -> None:
    controller = _controller(tmp_path, _SnapshotAdapter([]))
    actions = [Action(kind="block-apply", block_instance="i1", page="P1")]
    report = {
        "verdict": "pass",
        "stages": [
            {"name": "layout-lint", "status": "pass", "page": "P1"},
            {"name": "clusters", "status": "skipped", "page": "P1"},
            {"name": "check", "status": "pass", "page": "P1"},
            {"name": "bridge-check", "status": "skipped", "page": "P1"},
            {"name": "drc", "status": "skipped", "page": "P1"},
        ],
    }

    findings = controller._check_gate_contract(
        report,
        actions,
        1,
        gate_args=["sch", "gate", "--json", "--only", "layout-lint,check"],
    )

    assert findings == []


def test_gate_skip_removes_stage_from_required_set(tmp_path: Path) -> None:
    controller = _controller(tmp_path, _SnapshotAdapter([]))
    actions = [Action(kind="block-apply", block_instance="i1", page="P1")]
    report = {
        "verdict": "pass",
        "stages": [
            {"name": name, "status": "skipped" if name == "drc" else "pass", "page": "P1"}
            for name in ("layout-lint", "clusters", "check", "bridge-check", "drc")
        ],
    }

    findings = controller._check_gate_contract(
        report,
        actions,
        1,
        gate_args=["sch", "gate", "--json", "--skip", "drc"],
    )

    assert findings == []


def test_gate_only_and_skip_are_mutually_exclusive(tmp_path: Path) -> None:
    controller = _controller(tmp_path, _SnapshotAdapter([]))
    required, errors = controller._parse_gate_stage_args(
        ["sch", "gate", "--only", "layout-lint", "--skip", "drc"]
    )

    assert required == ("layout-lint",)
    assert any("不能同时" in error for error in errors)


def test_gate_all_pages_fills_only_missing_stage_page(tmp_path: Path) -> None:
    class GateAdapter(_SnapshotAdapter):
        def run_json(self, args):
            assert args[1] == "gate"
            return {"verdict": "pass", "stages": [{"name": "check", "status": "pass"}]}

    controller = _controller(tmp_path, GateAdapter([]))
    actions = [Action(kind="block-apply", block_instance="i1", page="P1")]

    report = controller._gate_all_pages(["sch", "gate", "--json"], actions, 1)

    assert report["stages"][0]["page"] == "P1"
    assert report["contract_errors"] == []


def test_gate_all_pages_preserves_correct_reported_page(tmp_path: Path) -> None:
    class GateAdapter(_SnapshotAdapter):
        def run_json(self, args):
            return {
                "verdict": "pass",
                "stages": [{"name": "check", "status": "pass", "page": "P1"}],
            }

    controller = _controller(tmp_path, GateAdapter([]))
    actions = [Action(kind="block-apply", block_instance="i1", page="P1")]

    report = controller._gate_all_pages(["sch", "gate", "--json"], actions, 1)

    assert report["stages"][0]["page"] == "P1"
    assert report["contract_errors"] == []


def test_gate_all_pages_rejects_wrong_reported_page_without_rewriting(tmp_path: Path) -> None:
    class GateAdapter(_SnapshotAdapter):
        def run_json(self, args):
            return {
                "verdict": "pass",
                "stages": [{"name": "check", "status": "pass", "page": "P2"}],
            }

    controller = _controller(tmp_path, GateAdapter([]))
    actions = [Action(kind="block-apply", block_instance="i1", page="P1")]

    report = controller._gate_all_pages(["sch", "gate", "--json"], actions, 1)

    assert report["verdict"] == "unknown"
    assert report["stages"][0]["page"] == "P2"
    assert any("报告 page='P2'" in error for error in report["contract_errors"])
    findings = controller._check_gate_contract(report, actions, 1)
    assert findings and findings[0].code == "GATE_UNVERIFIED"


@pytest.mark.parametrize(
    "status",
    ["ok", "success", "passed", True, 1],
)
def test_gate_contract_normalizes_positive_status_aliases(tmp_path: Path, status) -> None:
    controller = _controller(tmp_path, _SnapshotAdapter([]))
    actions = [Action(kind="block-apply", block_instance="i1", page="P1")]
    names = ("layout-lint", "clusters", "check", "bridge-check", "drc")
    report = {
        "verdict": "passed",
        "stages": [
            {"name": name, "status": status, "page": "P1"}
            for name in names
        ],
    }

    assert controller._check_gate_contract(report, actions, 1) == []


@pytest.mark.parametrize(
    "stage",
    [
        {"name": "check", "status": "success", "ok": False, "page": "P1"},
        {"name": "check", "status": "ok", "error": "connector timeout", "page": "P1"},
        {"name": "check", "passed": True, "failure": {"code": "E1"}, "page": "P1"},
    ],
)
def test_gate_contract_failure_payload_overrides_positive_alias(tmp_path: Path, stage: dict) -> None:
    controller = _controller(tmp_path, _SnapshotAdapter([]))
    actions = [Action(kind="block-apply", block_instance="i1", page="P1")]
    names = ("layout-lint", "clusters", "check", "bridge-check", "drc")
    report = {
        "verdict": "pass",
        "stages": [
            {"name": name, "status": "pass", "page": "P1"}
            for name in names
        ],
    }
    report["stages"][2] = stage

    findings = controller._check_gate_contract(report, actions, 1)

    assert findings and findings[0].code == "GATE_UNVERIFIED"


def test_gate_all_pages_normalizes_aliases_for_downstream_gauge(tmp_path: Path) -> None:
    class GateAdapter(_SnapshotAdapter):
        def run_json(self, args):
            return {
                "verdict": "passed",
                "stages": [
                    {"name": "layout-lint", "status": "ok"},
                    {"name": "clusters", "status": "success"},
                    {"name": "check", "status": "passed"},
                    {"name": "bridge-check", "ok": True},
                    {"name": "drc", "success": True},
                ],
            }

    controller = _controller(tmp_path, GateAdapter([]))
    actions = [Action(kind="block-apply", block_instance="i1", page="P1")]

    report = controller._gate_all_pages(["sch", "gate", "--json"], actions, 1)

    assert report["verdict"] == "pass"
    assert [stage["status"] for stage in report["stages"]] == ["pass"] * 5
    assert controller._check_gate_contract(report, actions, 1) == []


@pytest.mark.parametrize("alias", ["status", "ok", "success", "passed"])
def test_gate_all_pages_accepts_positive_envelope_alias(tmp_path: Path, alias: str) -> None:
    class GateAdapter(_SnapshotAdapter):
        def run_json(self, args):
            return {
                alias: True if alias in ("ok", "success", "passed") else "success",
                "stages": _gate_stage_report(
                    "layout-lint", "clusters", "check", "bridge-check", "drc"
                ),
            }

    controller = _controller(tmp_path, GateAdapter([]))
    actions = [Action(kind="block-apply", block_instance="i1", page="P1")]

    report = controller._gate_all_pages(["sch", "gate", "--json"], actions, 1)

    assert report["verdict"] == "pass"
    assert controller._check_gate_contract(report, actions, 1) == []


def test_gate_all_pages_top_level_ok_false_cannot_be_nominal_pass(tmp_path: Path) -> None:
    class GateAdapter(_SnapshotAdapter):
        def run_json(self, args):
            return {
                "ok": False,
                "verdict": "pass",
                "stages": _gate_stage_report(
                    "layout-lint", "clusters", "check", "bridge-check", "drc"
                ),
            }

    controller = _controller(tmp_path, GateAdapter([]))
    actions = [Action(kind="block-apply", block_instance="i1", page="P1")]

    report = controller._gate_all_pages(["sch", "gate", "--json"], actions, 1)

    assert report["verdict"] == "unknown"
    assert report["contract_errors"]


def test_gate_nonzero_rc_with_pass_is_unknown_and_unverified(tmp_path: Path) -> None:
    class NonzeroGateAdapter(_SnapshotAdapter):
        def run_json_with_rc(self, args):
            return (
                2,
                {
                    "verdict": "pass",
                    "stages": _gate_stage_report(
                        "layout-lint", "clusters", "check", "bridge-check", "drc"
                    ),
                },
                "checker emitted a report but exited non-zero",
            )

    controller = _controller(tmp_path, NonzeroGateAdapter([]), strict=True)
    actions = [Action(kind="block-apply", block_instance="i1", page="P1")]

    report = controller._gate_all_pages(["sch", "gate", "--json"], actions, 1)

    assert report["verdict"] == "unknown"
    assert any("rc=2" in error for error in report["contract_errors"])
    findings = controller._check_gate_contract(report, actions, 1)
    assert findings and findings[0].code == "GATE_UNVERIFIED"


def test_gate_nonzero_rc_with_fail_preserves_gate_fail(tmp_path: Path) -> None:
    class NonzeroGateAdapter(_SnapshotAdapter):
        def run_json_with_rc(self, args):
            return (
                2,
                {
                    "verdict": "fail",
                    "stages": [
                        {
                            "name": "check",
                            "status": "fail",
                            "error": "electrical violation",
                        }
                    ],
                },
                "checker found violations",
            )

    controller = _controller(tmp_path, NonzeroGateAdapter([]), strict=True)
    actions = [Action(kind="block-apply", block_instance="i1", page="P1")]

    report = controller._gate_all_pages(["sch", "gate", "--json"], actions, 1)

    assert report["verdict"] == "fail"
    # A command-level failure with a matching FAIL verdict is actionable gate
    # evidence, not a malformed response contract.
    assert report.get("contract_errors") == []
    assert controller._check_gate_contract(report, actions, 1) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": False, "error": "read failed", "result": {"components": [_part("U1", (1, 1, 2, 2))]}},
        {"result": {"ok": False, "error": "read failed", "components": [_part("U1", (1, 1, 2, 2))]}},
    ],
)
def test_list_components_rejects_explicit_failure_payload(tmp_path: Path, payload: dict) -> None:
    class ErrorListAdapter(_SnapshotAdapter):
        def run(self, args):
            if args[1] == "list":
                return 0, json.dumps(payload), ""
            return super().run(args)

    controller = _controller(tmp_path, ErrorListAdapter([]))

    with pytest.raises(ValueError, match="reports failure"):
        controller._list_components("P1", strict=True)


def test_list_components_rejects_explicit_failure_even_non_strict(tmp_path: Path) -> None:
    class ErrorListAdapter(_SnapshotAdapter):
        def run(self, args):
            if args[1] == "list":
                return 0, json.dumps({"ok": "false", "result": {"components": [_part("U1", (1, 1, 2, 2))]}}), ""
            return super().run(args)

    controller = _controller(tmp_path, ErrorListAdapter([]))

    with pytest.raises(ValueError, match="reports failure"):
        controller._list_components("P1")


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": False, "error": "clusters failed", "clusters": []},
        {"result": {"ok": False, "error": "clusters failed"}, "clusters": []},
        {"ok": True, "clusters": None},
    ],
)
def test_clusters_report_strict_rejects_failure_or_malformed_payload(tmp_path: Path, payload: dict) -> None:
    class ErrorClustersAdapter(_SnapshotAdapter):
        def run(self, args):
            if args[1] == "clusters":
                return 1, json.dumps(payload), "checker failed"
            return super().run(args)

    controller = _controller(tmp_path, ErrorClustersAdapter([]))

    with pytest.raises(ValueError):
        controller._clusters_report_strict("P1")


def test_clusters_report_strict_allows_nonzero_rc_with_valid_findings(tmp_path: Path) -> None:
    class FindingsClustersAdapter(_SnapshotAdapter):
        def run(self, args):
            if args[1] == "clusters":
                return 1, json.dumps({"clusters": [], "findings": [{"type": "overlap"}]}), ""
            return super().run(args)

    controller = _controller(tmp_path, FindingsClustersAdapter([]))

    report = controller._clusters_report_strict("P1")

    assert report["findings"]


def test_attribution_preserves_readback_and_marker_instructions() -> None:
    findings = [
        Finding(
            code="LAYOUT_READ_UNVERIFIED",
            where=Where(ref="P1"),
            evidence="list output was empty",
            suggested_fix_class="RETRY_READBACK",
        ),
        Finding(
            code="LAYOUT_DUPLICATE_MARKER",
            where=Where(ref="U1", pin="1", net="VCC"),
            evidence="two marker carriers",
            suggested_fix_class="DEDUPE_MARKER",
        ),
    ]

    feedback = attribute(findings)

    assert "回读证据不足" in feedback
    assert "标记整理" in feedback


class _DeliveryAdapter:
    def __init__(self, *, svg: bool = True, netlist: bool = True):
        self.svg = svg
        self.netlist = netlist

    def run(self, args):
        if args[1] == "export-image":
            if self.svg:
                out = args[args.index("--out") + 1]
                Path(out).write_text("<svg/>", encoding="utf-8")
            return 0, "", ""
        if args[1] == "netlist":
            return (0, '{"nets": []}', "") if self.netlist else (1, "", "netlist unavailable")
        raise AssertionError(f"unexpected delivery command: {args}")


def _delivery_controller(tmp_path: Path, adapter: _DeliveryAdapter) -> LoopController:
    controller = _controller(tmp_path, adapter, strict=False)
    controller.catalog = {"fixture": BlockRecord(block_id="fixture", name="fixture", desc="fixture")}
    return controller


def _delivery_result() -> LoopResult:
    return LoopResult(
        status="PASS",
        final_plan=BlockPlan(
            blocks=[PlannedBlock(block_id="fixture", instance="U1", page="P1")]
        ),
    )


@pytest.mark.parametrize(
    ("adapter", "missing"),
    [
        (_DeliveryAdapter(svg=False), "svg:P1"),
        (_DeliveryAdapter(netlist=False), "netlist"),
    ],
)
def test_delivery_missing_core_artifact_fails_closed(tmp_path: Path, adapter, missing: str) -> None:
    controller = _delivery_controller(tmp_path, adapter)
    result = _delivery_result()

    arts = controller.deliver(result)

    assert arts["ok"] is False
    assert missing in arts["missing"]
    assert result.status == "DELIVERY_FAIL"


def test_delivery_bom_failure_fails_closed(tmp_path: Path, monkeypatch) -> None:
    import edaloop.generate.bomcost as bomcost

    monkeypatch.setattr(bomcost, "summarize_bom", lambda _placed: (_ for _ in ()).throw(RuntimeError("boom")))
    controller = _delivery_controller(tmp_path, _DeliveryAdapter())
    result = _delivery_result()

    arts = controller.deliver(result)

    assert arts["ok"] is False
    assert "bom" in arts["missing"]
    assert result.status == "DELIVERY_FAIL"


def test_delivery_complete_contract_passes(tmp_path: Path) -> None:
    controller = _delivery_controller(tmp_path, _DeliveryAdapter())
    result = _delivery_result()

    arts = controller.deliver(result)

    assert arts["ok"] is True
    assert Path(arts["svg"]).stat().st_size > 0
    assert Path(arts["netlist"]).read_text(encoding="utf-8") == '{"nets": []}'
    assert result.status == "PASS"


def test_delivery_layout_review_required_is_not_promoted_to_pass(tmp_path: Path) -> None:
    controller = _delivery_controller(tmp_path, _DeliveryAdapter())
    result = _delivery_result()
    result.status = "LAYOUT_REVIEW_REQUIRED"
    result.review_required = True

    arts = controller.deliver(result)

    assert arts["ok"] is False
    assert arts["review_code"] == "LAYOUT_REVIEW_REQUIRED"
    assert "human-layout-review" in arts["missing"]
    assert result.status == "LAYOUT_REVIEW_REQUIRED"


def test_delivery_does_not_accept_stale_svg_when_exporter_writes_nothing(tmp_path: Path) -> None:
    # Simulate a reused run directory containing an old artifact.  A failed
    # exporter must remove/replace that path rather than silently reusing it.
    stale = tmp_path / "audit" / "delivery.svg"
    stale.parent.mkdir(parents=True)
    stale.write_text("<svg stale />", encoding="utf-8")
    controller = _delivery_controller(tmp_path, _DeliveryAdapter(svg=False))
    result = _delivery_result()

    arts = controller.deliver(result)

    assert arts["ok"] is False
    assert "svg:P1" in arts["missing"]
    assert not stale.exists()
    assert result.status == "DELIVERY_FAIL"


def test_case_writeback_skips_incomplete_delivery(tmp_path: Path, monkeypatch) -> None:
    from edaloop.generate.pipeline import _maybe_record_case

    monkeypatch.setattr("edaloop.generate.pipeline.get_embedder", lambda: FakeEmbedding())
    monkeypatch.setattr("edaloop.generate.pipeline.get_reranker", lambda: None)
    ir = DesignIR.model_validate(
        {
            "source": "customer.md",
            "functions": [{"name": "controller"}],
            "power": {"rails": [{"name": "VCC", "voltage": 3.3}]},
        }
    )
    ir.id = "delivery-gated"
    result = _delivery_result()
    audit = AuditLog(tmp_path / "audit")

    _maybe_record_case(
        ir,
        result,
        source="customer.md",
        dry_run=False,
        db_path=str(tmp_path / "cases.db"),
        audit=audit,
        delivery={"ok": False, "missing": ["svg:P1"]},
    )

    assert not (tmp_path / "cases.db").exists()
    events = [json.loads(line) for line in (tmp_path / "audit" / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e.get("kind") == "case-writeback-skip" for e in events)


def test_layout_snapshot_component_missing_pure_api() -> None:
    snapshot = {
        "page": "P1",
        "components": [{"designator": "U1", "bbox": _box(100, 300, 140, 340)}],
        "usableBand": _box(30, 30, 1140, 795),
        "readback": {"status": "ok"},
    }

    audit = audit_layout_snapshot(snapshot, expected_components=["U1", "C1"])

    assert any(f.code == LAYOUT_COMPONENT_MISSING and f.where.ref == "C1" for f in audit.findings)
