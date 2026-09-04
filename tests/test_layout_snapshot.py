from __future__ import annotations

import pytest

from edaloop.validate.layout import (
    ComponentSnapshot,
    InkSnapshot,
    LayoutSnapshot,
    MarkerSnapshot,
    PinSnapshot,
    Rect,
    LAYOUT_BODY_OVERLAP,
    LAYOUT_DUPLICATE_MARKER,
    LAYOUT_INK_OUT_OF_BAND,
    LAYOUT_MARKER_ON_BODY,
    LAYOUT_PAGE_INK_SPARSE,
    LAYOUT_REVIEW_REQUIRED,
    LAYOUT_PIN_COINCIDENCE,
    LAYOUT_PIN_NET_MISMATCH,
    LAYOUT_READ_UNVERIFIED,
    LAYOUT_SNAPSHOT_INVALID,
    audit_layout_snapshot,
    check_body_overlaps,
    check_duplicate_markers,
    check_ink_bounds,
    check_marker_body_overlaps,
    check_page_ink_sparse,
    check_pin_coincidences,
    check_pin_net_mismatches,
    page_ink_metrics,
)


def _component(
    ref: str,
    bbox: tuple[float, float, float, float],
    *pins: PinSnapshot,
    expected: dict[str, str] | None = None,
) -> ComponentSnapshot:
    return ComponentSnapshot(ref, Rect(*bbox), pins=tuple(pins), expected_pin_nets=expected or {})


def _snapshot(*components: ComponentSnapshot, markers=(), ink_boxes=(), **kwargs) -> LayoutSnapshot:
    return LayoutSnapshot(
        page="P1",
        components=components,
        markers=markers,
        ink_boxes=ink_boxes,
        usable_band=Rect(0, 0, 1000, 800),
        **kwargs,
    )


def test_body_overlap_is_strong_terminal_finding() -> None:
    snap = _snapshot(
        _component("R1", (100, 100, 220, 180)),
        _component("C1", (200, 140, 280, 220)),
    )

    findings = check_body_overlaps(snap)

    assert [f.code for f in findings] == [LAYOUT_BODY_OVERLAP]
    assert findings[0].weak is False
    assert findings[0].severity == "error"
    assert findings[0].where.ref == "C1"


def test_touching_bodies_are_not_overlap() -> None:
    snap = _snapshot(
        _component("R1", (0, 0, 100, 100)),
        _component("C1", (100, 0, 200, 100)),
    )

    assert check_body_overlaps(snap) == []


def test_pin_coincidence_and_duplicate_marker_are_independent_checks() -> None:
    snap = _snapshot(
        _component("U1", (100, 100, 180, 180), PinSnapshot("U1", "1", 120, 140, "A")),
        _component("R1", (220, 100, 300, 180), PinSnapshot("R1", "1", 120, 140, "B")),
        markers=(
            MarkerSnapshot(kind="netport", owner_ref="U1", pin="1", net="A", x=120, y=140, primitive_id="m1"),
            MarkerSnapshot(kind="netflag", owner_ref="U1", pin="1", net="A", x=120, y=160, primitive_id="m2"),
        ),
    )

    coincidence = check_pin_coincidences(snap)
    duplicate = check_duplicate_markers(snap)

    assert [f.code for f in coincidence] == [LAYOUT_PIN_COINCIDENCE]
    assert [f.code for f in duplicate] == [LAYOUT_DUPLICATE_MARKER]
    assert all(not f.weak for f in coincidence + duplicate)


def test_pin_net_mismatch_uses_expected_map_and_actual_pin_map() -> None:
    snap = _snapshot(
        _component(
            "U1",
            (100, 100, 180, 180),
            PinSnapshot("U1", "1", 120, 140, "WRONG"),
            expected={"1": "VCC"},
        ),
        pin_to_net={"U1:1": "WRONG"},
    )

    findings = check_pin_net_mismatches(snap)

    assert [f.code for f in findings] == [LAYOUT_PIN_NET_MISMATCH]
    assert findings[0].where.ref == "U1"
    assert findings[0].where.pin == "1"
    assert findings[0].where.net == "VCC"


def test_out_of_band_marker_and_wire_ink_are_blocking() -> None:
    snap = _snapshot(
        _component("R1", (100, 100, 180, 160)),
        markers=(
            MarkerSnapshot(
                kind="netport",
                owner_ref="R1",
                pin="1",
                net="A",
                ink_bbox=Rect(990, 150, 1020, 170),
            ),
        ),
        ink_boxes=(InkSnapshot(Rect(-10, 200, 20, 220), kind="wire", net="A"),),
    )

    findings = check_ink_bounds(snap)

    assert [f.code for f in findings] == [LAYOUT_INK_OUT_OF_BAND, LAYOUT_INK_OUT_OF_BAND]
    assert all(f.severity == "error" and not f.weak for f in findings)


def test_readback_failure_and_empty_components_never_pass() -> None:
    snap = LayoutSnapshot(
        page="P1",
        components=(),
        usable_band=Rect(0, 0, 1000, 800),
        readback_status="degraded",
        degraded=True,
        readback_error="sch list: empty components",
    )

    audit = audit_layout_snapshot(snap)
    codes = {f.code for f in audit.findings}

    assert not audit.ok
    assert not audit.verified
    assert LAYOUT_READ_UNVERIFIED in codes
    assert LAYOUT_SNAPSHOT_INVALID in codes
    assert all(not f.weak for f in audit.findings)


def test_unknown_component_type_cannot_promote_snapshot_to_verified() -> None:
    audit = audit_layout_snapshot(
        {
            "page": "P1",
            "components": [
                {
                    "designator": "mystery-1",
                    "componentType": "mystery",
                    "bbox": {"minX": 10, "minY": 10, "maxX": 20, "maxY": 20},
                    "pins": [],
                }
            ],
            "sheetUsable": {"minX": 0, "minY": 0, "maxX": 100, "maxY": 100},
            "readbackStatus": "verified",
        }
    )

    assert audit.verified is False
    assert any("unknown component type" in f.evidence for f in audit.findings)


@pytest.mark.parametrize(
    ("component_type", "expected_kind"),
    [("net-port", "netport"), ("net_flag", "netflag"), ("NET-LABEL", "netlabel")],
)
def test_layout_snapshot_normalizes_marker_aliases_directly(
    component_type: str, expected_kind: str
) -> None:
    audit = audit_layout_snapshot(
        {
            "page": "P1",
            "components": [
                {
                    "componentType": "part",
                    "designator": "U1",
                    "bbox": {"minX": 10, "minY": 10, "maxX": 20, "maxY": 20},
                    "pins": [],
                },
                {
                    "componentType": component_type,
                    "net": "VCC",
                    "ownerRef": "U1",
                    "pin": "1",
                    "x": 30,
                    "y": 15,
                },
            ],
            "sheetUsable": {"minX": 0, "minY": 0, "maxX": 100, "maxY": 100},
            "readbackStatus": "verified",
        }
    )

    assert audit.verified is True
    assert [marker.kind for marker in audit.snapshot.markers] == [expected_kind]


def test_mapping_input_round_trips_and_clean_snapshot_passes() -> None:
    raw = {
        "page": "P2",
        "components": [
            {
                "designator": "R1",
                "componentType": "part",
                "bbox": {"minX": 10, "minY": 20, "maxX": 40, "maxY": 50},
                "pins": [{"pinNumber": "1", "x": 10, "y": 35, "net": "A"}],
            }
        ],
        "markers": [{"componentType": "netport", "ownerRef": "R1", "pin": "1", "net": "A", "x": 5, "y": 35}],
        "sheetUsable": {"minX": 0, "minY": 0, "maxX": 100, "maxY": 100},
        "readbackStatus": "verified",
    }

    audit = audit_layout_snapshot(raw)

    assert audit.ok
    assert audit.snapshot is not None
    assert audit.snapshot.page == "P2"
    assert audit.snapshot.components[0].pins[0].key == "R1:1"
    dumped = audit.snapshot.to_dict()
    assert dumped["usableBand"]["maxX"] == 100.0
    assert dumped["components"][0]["designator"] == "R1"


def test_allow_oversize_is_explicit_and_does_not_hide_readback_errors() -> None:
    snap = _snapshot(
        _component("U1", (-100, 0, 1100, 700)),
        oversize=True,
        readback_status="ok",
    )

    audit = audit_layout_snapshot(snap, allow_oversize=True)
    assert audit.ok

    failed = audit_layout_snapshot(
        LayoutSnapshot(
            page="P1",
            components=(),
            usable_band=Rect(0, 0, 1000, 800),
            oversize=True,
            readback_status="error",
        ),
        allow_oversize=True,
    )
    assert any(f.code == LAYOUT_READ_UNVERIFIED for f in failed.findings)


def test_marker_on_body_is_a_visible_weak_warning() -> None:
    snap = _snapshot(
        _component("U1", (100, 100, 180, 180)),
        markers=(
            MarkerSnapshot(
                kind="netport",
                owner_ref="U1",
                pin="1",
                net="VCC",
                ink_bbox=Rect(150, 150, 205, 170),
            ),
        ),
    )

    findings = check_marker_body_overlaps(snap)

    assert len(findings) == 1
    assert findings[0].code == LAYOUT_MARKER_ON_BODY
    assert findings[0].weak is True
    assert findings[0].severity == "warn"
    audit = audit_layout_snapshot(snap)
    assert audit.ok is True
    assert any(f.code == LAYOUT_MARKER_ON_BODY for f in audit.findings)
    assert audit.review_required is True
    assert audit.review_code == LAYOUT_REVIEW_REQUIRED
    assert audit.to_dict()["reviewRequired"] is True
    assert audit.to_dict()["reviewCode"] == LAYOUT_REVIEW_REQUIRED


def test_marker_point_without_ink_bbox_does_not_invent_overlap() -> None:
    snap = _snapshot(
        _component("U1", (100, 100, 180, 180)),
        markers=(MarkerSnapshot(kind="netport", owner_ref="U1", pin="1", net="VCC", x=150, y=150),),
    )

    assert check_marker_body_overlaps(snap) == []


def test_sparse_page_warning_uses_union_ink_and_skips_last_page() -> None:
    snap = _snapshot(_component("R1", (100, 100, 120, 120)), readback_status="verified")

    metrics = page_ink_metrics(snap)
    assert metrics["occupancy"] == pytest.approx(0.0005)
    findings = check_page_ink_sparse(snap, is_last_page=False)
    assert len(findings) == 1
    assert findings[0].code == LAYOUT_PAGE_INK_SPARSE
    assert findings[0].weak is True
    assert check_page_ink_sparse(snap, is_last_page=True) == []
    assert audit_layout_snapshot(snap, is_last_page=False).ok is True
    sparse_audit = audit_layout_snapshot(snap, is_last_page=False)
    # Sparse-page occupancy is a weak packing signal; it does not imply that
    # a human visual review is required.  Review status is reserved for
    # marker/body overlap, where the connector's rendered geometry can be
    # ambiguous.
    assert sparse_audit.review_required is False
    assert sparse_audit.review_code == ""


def test_pin_identity_strips_qualified_prefix_even_with_explicit_owner() -> None:
    pin = PinSnapshot.from_mapping(
        {"ref": "U1", "pinNumber": "U1:1", "x": 1, "y": 2},
    )
    assert pin.ref == "U1"
    assert pin.pin == "1"
    assert pin.key == "U1:1"

    marker = MarkerSnapshot.from_mapping(
        {"ownerRef": "U1", "pin": "U1:1", "net": "VCC", "x": 1, "y": 2},
    )
    assert marker.pin_ref == "U1:1"


def test_expected_alias_maps_are_serialized_flat_and_deterministically() -> None:
    snap = _snapshot(
        _component("U1", (10, 10, 20, 20)),
        expected_pin_to_net={"U2": {"2": "GND"}, "U1:1": "VCC"},
        expected_nets={"P2": {"Z", "A"}, "P1": {"VCC": True, "GND": True}},
    )

    dumped = snap.to_dict()

    assert dumped["expectedPinToNet"] == {"U1:1": "VCC", "U2:2": "GND"}
    assert dumped["expectedNets"] == {
        "P1": {"GND": True, "VCC": True},
        "P2": ["A", "Z"],
    }

    restored = LayoutSnapshot.from_mapping(dumped)
    assert restored.expected_pin_to_net == snap.expected_pin_to_net
    assert restored.expected_nets == snap.expected_nets


def test_conflicting_canonical_pin_keys_invalidate_snapshot() -> None:
    snap = _snapshot(
        _component("U1", (10, 10, 20, 20)),
        expected_pin_to_net={"U1": {"1": "VCC"}, "U1:1": "GND"},
    )

    assert not snap.verified_readback
    assert any("canonical pin key collision" in error for error in snap.validation_errors)
    audit = audit_layout_snapshot(snap)
    assert any(f.code == LAYOUT_SNAPSHOT_INVALID for f in audit.findings)


def test_validation_errors_survive_nested_snapshot_round_trip() -> None:
    raw = {
        "page": "P1",
        "components": [
            {
                "designator": "U1",
                "componentType": "part",
                "bbox": {"minX": 0, "minY": 0, "maxX": 10, "maxY": 10},
                "pins": [
                    {
                        "pin": "U1:1",
                        "x": 1,
                        "y": 2,
                        "validationErrors": ["pin-source"],
                    },
                ],
                "validationErrors": ["component-source"],
            },
        ],
        "markers": [
            {
                "ownerRef": "U1",
                "pin": "U1:1",
                "x": 1,
                "y": 2,
                "validationErrors": ["marker-source"],
            },
        ],
        "inkBoxes": [
            {
                "bbox": {"minX": 0, "minY": 0, "maxX": 1, "maxY": 1},
                "validationErrors": ["ink-source"],
            },
        ],
        "sheetUsable": {"minX": 0, "minY": 0, "maxX": 100, "maxY": 100},
        "readback": {"status": "ok"},
        "validationErrors": ["snapshot-source"],
    }

    first = LayoutSnapshot.from_mapping(raw)
    second = LayoutSnapshot.from_mapping(first.to_dict())

    assert first.validation_errors == second.validation_errors == ("snapshot-source",)
    assert first.components[0].validation_errors == second.components[0].validation_errors == ("component-source",)
    assert first.components[0].pins[0].validation_errors == second.components[0].pins[0].validation_errors == ("pin-source",)
    assert first.markers[0].validation_errors == second.markers[0].validation_errors == ("marker-source",)
    assert first.ink_boxes[0].validation_errors == second.ink_boxes[0].validation_errors == ("ink-source",)


@pytest.mark.parametrize(
    ("payload", "verified"),
    [
        ({"ok": True}, True),
        ({"ok": "true"}, True),
        ({"readback": {"ok": True}}, True),
        ({"readback": {"status": "success"}}, True),
        ({"readbackStatus": False}, False),
        ({"ok": "false"}, False),
        ({}, False),
    ],
)
def test_raw_readback_status_and_ok_flags_fail_closed(payload: dict, verified: bool) -> None:
    snapshot = LayoutSnapshot.from_mapping({"page": "P1", **payload})
    assert snapshot.verified_readback is verified


def test_outer_failure_envelope_cannot_be_overwritten_by_nested_success() -> None:
    snapshot = LayoutSnapshot.from_mapping(
        {
            "page": "P1",
            "ok": False,
            "result": {
                "ok": True,
                "components": [],
                "sheetUsable": {"minX": 0, "minY": 0, "maxX": 100, "maxY": 100},
            },
        }
    )

    assert snapshot.verified_readback is False
    assert snapshot.readback_status == "error"
    assert any("outer envelope reports failure" in error for error in snapshot.validation_errors)


def test_malformed_readback_shape_is_recorded_and_unverified() -> None:
    snapshot = LayoutSnapshot.from_mapping({"page": "P1", "readback": ["stale"]})
    assert snapshot.verified_readback is False
    assert "readback:expected object or status" in snapshot.validation_errors


def test_string_boolean_flags_are_not_truthiness_coerced() -> None:
    good = LayoutSnapshot.from_mapping(
        {"page": "P1", "status": "ok", "degraded": "false", "oversize": "false"}
    )
    assert good.verified_readback is True
    assert good.degraded is False
    assert good.oversize is False

    malformed = LayoutSnapshot.from_mapping(
        {"page": "P1", "status": "ok", "degraded": "maybe"}
    )
    assert malformed.verified_readback is False
    assert "degraded flag is malformed" in malformed.validation_errors


def test_sheet_pseudo_components_are_not_counted_as_parts() -> None:
    snapshot = LayoutSnapshot.from_mapping(
        {
            "page": "P1",
            "components": [
                {"componentType": "sheet", "designator": "S1"},
                {
                    "componentType": "part",
                    "designator": "R1",
                    "bbox": {"minX": 0, "minY": 0, "maxX": 10, "maxY": 10},
                },
            ],
            "sheetUsable": {"minX": 0, "minY": 0, "maxX": 100, "maxY": 100},
            "status": "ok",
        }
    )
    assert [component.ref for component in snapshot.components] == ["R1"]


def test_malformed_collection_fields_become_structural_errors_not_type_errors() -> None:
    snapshot = LayoutSnapshot.from_mapping(
        {
            "page": "P1",
            "components": 3,
            "markers": {"bad": True},
            "inkBoxes": "not-a-list",
            "sheetUsable": {"minX": 0, "minY": 0, "maxX": 100, "maxY": 100},
            "status": "ok",
        }
    )

    assert snapshot.verified_readback is False
    assert {
        "components:expected sequence",
        "markers:expected sequence",
        "ink_boxes:expected sequence",
    }.issubset(set(snapshot.validation_errors))
    audit = audit_layout_snapshot(
        {
            "page": "P1",
            "components": [{"designator": "U1", "bbox": {"minX": 0, "minY": 0, "maxX": 10, "maxY": 10}}],
            "markers": 1,
            "sheetUsable": {"minX": 0, "minY": 0, "maxX": 100, "maxY": 100},
            "status": "ok",
        }
    )
    assert not audit.ok
    assert any(f.code == LAYOUT_SNAPSHOT_INVALID for f in audit.findings)


def test_malformed_component_pins_are_recorded_without_raising() -> None:
    component = ComponentSnapshot.from_mapping(
        {
            "designator": "U1",
            "bbox": {"minX": 0, "minY": 0, "maxX": 10, "maxY": 10},
            "pins": 1,
        }
    )
    assert "pins:expected sequence" in component.validation_errors


def test_unverified_snapshot_does_not_derive_stale_geometry_findings() -> None:
    raw = {
        "page": "P1",
        "components": [
            {"designator": "U1", "bbox": {"minX": 0, "minY": 0, "maxX": 20, "maxY": 20}},
            {"designator": "U2", "bbox": {"minX": 10, "minY": 10, "maxX": 30, "maxY": 30}},
        ],
        "sheetUsable": {"minX": 0, "minY": 0, "maxX": 100, "maxY": 100},
        "readbackStatus": "error",
        "expectedNets": {"P1": ["VCC"]},
    }

    audit = audit_layout_snapshot(raw)

    assert not audit.ok
    assert {finding.code for finding in audit.findings} == {LAYOUT_READ_UNVERIFIED}
