from __future__ import annotations

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
    LAYOUT_PIN_COINCIDENCE,
    LAYOUT_PIN_NET_MISMATCH,
    LAYOUT_READ_UNVERIFIED,
    LAYOUT_SNAPSHOT_INVALID,
    audit_layout_snapshot,
    check_body_overlaps,
    check_duplicate_markers,
    check_ink_bounds,
    check_pin_coincidences,
    check_pin_net_mismatches,
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
