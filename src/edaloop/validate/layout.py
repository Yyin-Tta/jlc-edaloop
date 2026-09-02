"""Deterministic, platform-independent layout snapshot checks.

The EasyEDA connector is responsible for collecting a :class:`LayoutSnapshot`;
this module deliberately does not perform any I/O.  Keeping the checks pure
makes the terminal gate reproducible with a small fake geometry fixture and
prevents a failed ``sch list`` call from being mistaken for an empty page.

There are three intentionally separate geometry/electrical notions here:

* ``bbox`` is the real component body (the object used for body collision);
* ``ink_bbox`` is rendered ink (markers, labels, wires, and optional body ink);
* ``PinSnapshot.net`` is electrical readback, compared with an optional
  expected pin-to-net map.

All findings produced by the terminal checks are strong errors.  A caller may
choose to downgrade a *known* oversize page by passing ``allow_oversize=True``;
that exception is explicit and never applies to readback or pin/net checks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from math import hypot, isfinite
from typing import Any, Iterable

from edaloop.validate.models import Finding, Where


# Public codes.  Keeping them in one place lets controller/audit code avoid
# spelling drift while retaining the existing Finding model contract.
LAYOUT_BODY_OVERLAP = "LAYOUT_BODY_OVERLAP"
LAYOUT_PIN_COINCIDENCE = "LAYOUT_PIN_COINCIDENCE"
LAYOUT_DUPLICATE_MARKER = "LAYOUT_DUPLICATE_MARKER"
LAYOUT_PIN_NET_MISMATCH = "LAYOUT_PIN_NET_MISMATCH"
LAYOUT_INK_OUT_OF_BAND = "LAYOUT_INK_OUT_OF_BAND"
LAYOUT_TITLEBLOCK_OCCLUDE = "LAYOUT_TITLEBLOCK_OCCLUDE"
LAYOUT_NET_MISSING = "LAYOUT_NET_MISSING"
LAYOUT_READ_UNVERIFIED = "LAYOUT_READ_UNVERIFIED"
LAYOUT_SNAPSHOT_INVALID = "LAYOUT_SNAPSHOT_INVALID"

# A few aliases are useful to integrations that use shorter terminology.
BODY_OVERLAP = LAYOUT_BODY_OVERLAP
PIN_COINCIDENCE = LAYOUT_PIN_COINCIDENCE
DUPLICATE_MARKER = LAYOUT_DUPLICATE_MARKER
PIN_NET_MISMATCH = LAYOUT_PIN_NET_MISMATCH
INK_OUT_OF_BAND = LAYOUT_INK_OUT_OF_BAND
READ_UNVERIFIED = LAYOUT_READ_UNVERIFIED
SNAPSHOT_INVALID = LAYOUT_SNAPSHOT_INVALID
NET_MISSING = LAYOUT_NET_MISSING

SNAPSHOT_VERSION = "1"
_GOOD_READBACK = {"ok", "verified", "complete", "success"}


def _as_float(value: Any) -> float:
    """Convert a coordinate without hiding malformed values."""

    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _first(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def _rect_values(value: Any) -> tuple[float, float, float, float] | None:
    if isinstance(value, Rect):
        return value.min_x, value.min_y, value.max_x, value.max_y
    if isinstance(value, Mapping):
        vals = (
            _first(value, "min_x", "minX", "left", "x1"),
            _first(value, "min_y", "minY", "bottom", "y1"),
            _first(value, "max_x", "maxX", "right", "x2"),
            _first(value, "max_y", "maxY", "top", "y2"),
        )
        if all(v is not None for v in vals):
            return tuple(_as_float(v) for v in vals)  # type: ignore[return-value]
        # Some APIs return {x, y, width, height} for an ink rectangle.
        x = _first(value, "x")
        y = _first(value, "y")
        w = _first(value, "width", "w")
        h = _first(value, "height", "h")
        if all(v is not None for v in (x, y, w, h)):
            fx, fy, fw, fh = map(_as_float, (x, y, w, h))
            return fx, fy, fx + fw, fy + fh
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) >= 4:
            return tuple(_as_float(value[i]) for i in range(4))  # type: ignore[return-value]
    # Be liberal when adapting a connector response object.
    if value is not None:
        vals = tuple(getattr(value, k, None) for k in ("min_x", "min_y", "max_x", "max_y"))
        if all(v is not None for v in vals):
            return tuple(_as_float(v) for v in vals)  # type: ignore[return-value]
        vals = tuple(getattr(value, k, None) for k in ("minX", "minY", "maxX", "maxY"))
        if all(v is not None for v in vals):
            return tuple(_as_float(v) for v in vals)  # type: ignore[return-value]
    return None


@dataclass(frozen=True, slots=True)
class Rect:
    """Axis-aligned rectangle in the EasyEDA page coordinate system."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "min_x", _as_float(self.min_x))
        object.__setattr__(self, "min_y", _as_float(self.min_y))
        object.__setattr__(self, "max_x", _as_float(self.max_x))
        object.__setattr__(self, "max_y", _as_float(self.max_y))

    @classmethod
    def from_value(cls, value: Any) -> "Rect":
        vals = _rect_values(value)
        if vals is None:
            raise ValueError("rectangle requires minX/minY/maxX/maxY (or x/y/width/height)")
        return cls(*vals)

    @property
    def valid(self) -> bool:
        return (
            all(isfinite(v) for v in (self.min_x, self.min_y, self.max_x, self.max_y))
            and self.min_x <= self.max_x
            and self.min_y <= self.max_y
        )

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    def intersects(self, other: "Rect", tolerance: float = 0.0) -> bool:
        """Return true only for positive-area intersection.

        Touching edges are not considered a body collision.  ``tolerance`` is
        subtracted from each overlap axis and is therefore useful for the
        connector's coordinate rounding noise.
        """

        tolerance = max(0.0, float(tolerance))
        if not self.valid or not other.valid:
            return False
        return (
            min(self.max_x, other.max_x) - max(self.min_x, other.min_x) > tolerance
            and min(self.max_y, other.max_y) - max(self.min_y, other.min_y) > tolerance
        )

    def outside(self, container: "Rect", tolerance: float = 0.0) -> bool:
        tolerance = max(0.0, float(tolerance))
        if not self.valid or not container.valid:
            return True
        return (
            self.min_x < container.min_x - tolerance
            or self.min_y < container.min_y - tolerance
            or self.max_x > container.max_x + tolerance
            or self.max_y > container.max_y + tolerance
        )

    def to_dict(self) -> dict[str, float]:
        # Include the upstream API's camelCase spelling in serialized audit
        # records; Python callers still use snake_case attributes.
        return {
            "minX": self.min_x,
            "minY": self.min_y,
            "maxX": self.max_x,
            "maxY": self.max_y,
        }

    def text(self) -> str:
        return f"({self.min_x:g},{self.min_y:g})-({self.max_x:g},{self.max_y:g})"


@dataclass(frozen=True, slots=True)
class PinSnapshot:
    """One pin as returned by ``sch list --include-pins``."""

    ref: str
    pin: str
    x: float
    y: float
    net: str = ""
    expected_net: str | None = None
    primitive_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "ref", str(self.ref or ""))
        object.__setattr__(self, "pin", str(self.pin or ""))
        object.__setattr__(self, "x", _as_float(self.x))
        object.__setattr__(self, "y", _as_float(self.y))
        object.__setattr__(self, "net", str(self.net or ""))
        if self.expected_net is not None:
            object.__setattr__(self, "expected_net", str(self.expected_net))
        object.__setattr__(self, "primitive_id", str(self.primitive_id or ""))

    @property
    def key(self) -> str:
        return f"{self.ref}:{self.pin}"

    @property
    def pin_number(self) -> str:
        return self.pin

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, ref: str = "") -> "PinSnapshot":
        raw_pin = _first(value, "pin", "pinNumber", "pin_number", "number", "name", default="")
        owner = str(_first(value, "ref", "designator", "ownerRef", default=ref) or ref)
        # A few connector responses identify a pin as ``R1:1`` rather than
        # carrying owner and number separately.
        if not owner and isinstance(raw_pin, str) and ":" in raw_pin:
            owner, raw_pin = raw_pin.split(":", 1)
        return cls(
            ref=owner,
            pin=str(raw_pin or ""),
            x=_first(value, "x", "X", default=float("nan")),
            y=_first(value, "y", "Y", default=float("nan")),
            net=_first(value, "net", "netName", "actualNet", default="") or "",
            expected_net=_first(value, "expected_net", "expectedNet", default=None),
            primitive_id=_first(value, "primitiveId", "primitive_id", "id", default="") or "",
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ref": self.ref,
            "pin": self.pin,
            "x": self.x,
            "y": self.y,
            "net": self.net,
        }
        if self.expected_net is not None:
            out["expectedNet"] = self.expected_net
        if self.primitive_id:
            out["primitiveId"] = self.primitive_id
        return out


@dataclass(frozen=True, slots=True)
class ComponentSnapshot:
    """A real component/body plus its pin readback."""

    ref: str
    bbox: Rect | Any | None = None
    pins: tuple[PinSnapshot, ...] | Sequence[Any] = ()
    ink_bbox: Rect | Any | None = None
    component_type: str = "part"
    primitive_id: str = ""
    expected_pin_nets: Mapping[str, str] = field(default_factory=dict)
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "ref", str(self.ref or ""))
        errors = list(self.validation_errors or ())
        for attr, label in (("bbox", "bbox"), ("ink_bbox", "ink_bbox")):
            value = getattr(self, attr)
            if value is None or isinstance(value, Rect):
                continue
            try:
                object.__setattr__(self, attr, Rect.from_value(value))
            except ValueError as exc:
                errors.append(f"{label}:{exc}")
                object.__setattr__(self, attr, None)
        converted: list[PinSnapshot] = []
        for i, pin in enumerate(self.pins or ()):
            if isinstance(pin, PinSnapshot):
                converted.append(pin if pin.ref else PinSnapshot(self.ref, pin.pin, pin.x, pin.y, pin.net, pin.expected_net, pin.primitive_id))
                continue
            if isinstance(pin, Mapping):
                converted.append(PinSnapshot.from_mapping(pin, ref=self.ref))
                continue
            errors.append(f"pin[{i}]:unsupported value")
        converted.sort(key=lambda p: (p.ref, p.pin, p.net.casefold(), p.x, p.y, p.primitive_id))
        object.__setattr__(self, "pins", tuple(converted))
        object.__setattr__(self, "component_type", str(self.component_type or "part"))
        object.__setattr__(self, "primitive_id", str(self.primitive_id or ""))
        object.__setattr__(self, "expected_pin_nets", {str(k): str(v) for k, v in (self.expected_pin_nets or {}).items()})
        object.__setattr__(self, "validation_errors", tuple(errors))

    @property
    def designator(self) -> str:
        return self.ref

    @property
    def body_bbox(self) -> Rect | None:
        return self.bbox if isinstance(self.bbox, Rect) else None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ComponentSnapshot":
        ref = str(_first(value, "ref", "designator", "name", "id", default="") or "")
        expected = _first(value, "expected_pin_nets", "expectedPinNets", "pinNets", default={}) or {}
        return cls(
            ref=ref,
            bbox=_first(value, "bbox", "body", "box", "body_bbox", "bodyBBox", default=None),
            pins=_first(value, "pins", "pinList", default=()) or (),
            ink_bbox=_first(value, "ink_bbox", "inkBBox", "ink", default=None),
            component_type=_first(value, "component_type", "componentType", "type", default="part") or "part",
            primitive_id=_first(value, "primitive_id", "primitiveId", default="") or "",
            expected_pin_nets=expected if isinstance(expected, Mapping) else {},
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "designator": self.ref,
            "componentType": self.component_type,
            "bbox": self.bbox.to_dict() if isinstance(self.bbox, Rect) else None,
            "pins": [p.to_dict() for p in self.pins],
        }
        if isinstance(self.ink_bbox, Rect):
            out["inkBBox"] = self.ink_bbox.to_dict()
        if self.primitive_id:
            out["primitiveId"] = self.primitive_id
        if self.expected_pin_nets:
            out["expectedPinNets"] = dict(self.expected_pin_nets)
        if self.validation_errors:
            out["validationErrors"] = list(self.validation_errors)
        return out


@dataclass(frozen=True, slots=True)
class MarkerSnapshot:
    """Rendered net marker (netport/netflag/netlabel) and its ownership."""

    kind: str = "netport"
    net: str = ""
    owner_ref: str = ""
    pin: str = ""
    ink_bbox: Rect | Any | None = None
    x: float | None = None
    y: float | None = None
    primitive_id: str = ""
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", str(self.kind or "netport"))
        object.__setattr__(self, "net", str(self.net or ""))
        object.__setattr__(self, "owner_ref", str(self.owner_ref or ""))
        object.__setattr__(self, "pin", str(self.pin or ""))
        object.__setattr__(self, "x", None if self.x is None else _as_float(self.x))
        object.__setattr__(self, "y", None if self.y is None else _as_float(self.y))
        object.__setattr__(self, "primitive_id", str(self.primitive_id or ""))
        errors = list(self.validation_errors or ())
        if self.ink_bbox is not None and not isinstance(self.ink_bbox, Rect):
            try:
                object.__setattr__(self, "ink_bbox", Rect.from_value(self.ink_bbox))
            except ValueError as exc:
                errors.append(f"ink_bbox:{exc}")
                object.__setattr__(self, "ink_bbox", None)
        object.__setattr__(self, "validation_errors", tuple(errors))

    @property
    def ref(self) -> str:
        return self.owner_ref

    @property
    def pin_ref(self) -> str:
        return f"{self.owner_ref}:{self.pin}" if self.owner_ref and self.pin else ""

    @property
    def point(self) -> tuple[float, float] | None:
        if self.x is None or self.y is None or not isfinite(self.x) or not isfinite(self.y):
            return None
        return self.x, self.y

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MarkerSnapshot":
        owner = str(_first(value, "owner_ref", "ownerRef", "ref", "designator", "owner", default="") or "")
        pin = str(_first(value, "pin", "pinNumber", "pin_number", "ownerPin", default="") or "")
        pin_ref = _first(value, "pinRef", "pin_ref", default="")
        if (not owner or not pin) and isinstance(pin, str) and ":" in pin:
            owner, pin = pin.split(":", 1)
        if (not owner or not pin) and isinstance(pin_ref, str) and ":" in pin_ref:
            owner, pin = pin_ref.split(":", 1)
        return cls(
            kind=_first(value, "kind", "componentType", "type", default="netport") or "netport",
            net=_first(value, "net", "name", "netName", default="") or "",
            owner_ref=owner,
            pin=pin,
            ink_bbox=_first(value, "ink_bbox", "inkBBox", "bbox", "box", default=None),
            x=_first(value, "x", "X", default=None),
            y=_first(value, "y", "Y", default=None),
            primitive_id=_first(value, "primitive_id", "primitiveId", "id", default="") or "",
        )

    def effective_bbox(self) -> Rect | None:
        if isinstance(self.ink_bbox, Rect):
            return self.ink_bbox
        if self.point is not None:
            return Rect(self.point[0], self.point[1], self.point[0], self.point[1])
        return None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "net": self.net,
            "ownerRef": self.owner_ref,
            "pin": self.pin,
        }
        if isinstance(self.ink_bbox, Rect):
            out["inkBBox"] = self.ink_bbox.to_dict()
        if self.x is not None:
            out["x"] = self.x
        if self.y is not None:
            out["y"] = self.y
        if self.primitive_id:
            out["primitiveId"] = self.primitive_id
        if self.validation_errors:
            out["validationErrors"] = list(self.validation_errors)
        return out


@dataclass(frozen=True, slots=True)
class InkSnapshot:
    """Optional non-marker rendered ink, usually a wire or label primitive."""

    bbox: Rect | Any
    kind: str = "ink"
    ref: str = ""
    net: str = ""
    primitive_id: str = ""
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        errors = list(self.validation_errors or ())
        if not isinstance(self.bbox, Rect):
            try:
                object.__setattr__(self, "bbox", Rect.from_value(self.bbox))
            except ValueError as exc:
                errors.append(f"bbox:{exc}")
                object.__setattr__(self, "bbox", None)
        object.__setattr__(self, "kind", str(self.kind or "ink"))
        object.__setattr__(self, "ref", str(self.ref or ""))
        object.__setattr__(self, "net", str(self.net or ""))
        object.__setattr__(self, "primitive_id", str(self.primitive_id or ""))
        object.__setattr__(self, "validation_errors", tuple(errors))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "InkSnapshot":
        return cls(
            bbox=_first(value, "bbox", "ink_bbox", "inkBBox", default=None),
            kind=_first(value, "kind", "type", default="ink") or "ink",
            ref=_first(value, "ref", "designator", default="") or "",
            net=_first(value, "net", "name", default="") or "",
            primitive_id=_first(value, "primitiveId", "primitive_id", "id", default="") or "",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "net": self.net,
            "bbox": self.bbox.to_dict() if isinstance(self.bbox, Rect) else None,
            **({"primitiveId": self.primitive_id} if self.primitive_id else {}),
            **({"validationErrors": list(self.validation_errors)} if self.validation_errors else {}),
        }


def _coerce_component(value: Any) -> ComponentSnapshot:
    if isinstance(value, ComponentSnapshot):
        return value
    if isinstance(value, Mapping):
        return ComponentSnapshot.from_mapping(value)
    raise ValueError("component must be a ComponentSnapshot or mapping")


def _coerce_marker(value: Any) -> MarkerSnapshot:
    if isinstance(value, MarkerSnapshot):
        return value
    if isinstance(value, Mapping):
        return MarkerSnapshot.from_mapping(value)
    raise ValueError("marker must be a MarkerSnapshot or mapping")


def _coerce_ink(value: Any) -> InkSnapshot:
    if isinstance(value, InkSnapshot):
        return value
    if isinstance(value, Mapping):
        return InkSnapshot.from_mapping(value)
    # A bare rectangle is a useful shorthand in tests/adapters.
    return InkSnapshot(bbox=value)


@dataclass(frozen=True, slots=True)
class LayoutSnapshot:
    """Immutable page readback consumed by the terminal layout checks.

    ``expected_pin_nets`` is the preferred expected map.  The two additional
    names are accepted as compatibility aliases because older controller code
    used ``expected_pin_to_net`` and ``expected_nets`` in experiments.
    """

    page: str
    components: tuple[ComponentSnapshot, ...] | Sequence[Any] = ()
    markers: tuple[MarkerSnapshot, ...] | Sequence[Any] = ()
    ink_boxes: tuple[InkSnapshot, ...] | Sequence[Any] = ()
    usable_band: Rect | Any | None = None
    titleblock_keepout: Rect | Any | None = None
    pin_to_net: Mapping[Any, Any] = field(default_factory=dict)
    expected_pin_nets: Mapping[Any, Any] = field(default_factory=dict)
    expected_pin_to_net: Mapping[Any, Any] = field(default_factory=dict)
    expected_nets: Mapping[Any, Any] = field(default_factory=dict)
    tool: str = ""
    connector: str = ""
    tool_version: str = ""
    connector_version: str = ""
    snapshot_version: str = SNAPSHOT_VERSION
    readback_status: str = "ok"
    degraded: bool = False
    readback_error: str = ""
    oversize: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        errors = list(self.validation_errors or ())
        object.__setattr__(self, "page", str(self.page or ""))
        converted_components: list[ComponentSnapshot] = []
        converted_component_markers: list[MarkerSnapshot] = []
        for i, item in enumerate(self.components or ()):
            try:
                component = _coerce_component(item)
                if component.component_type.casefold() in {"netport", "netflag", "netlabel", "marker"}:
                    # ``sch list`` commonly returns markers in the same
                    # components array as parts.  Normalize them here so
                    # callers do not have to know which connector variant was
                    # used.  Marker fields are intentionally read from the
                    # original mapping when available (component snapshots
                    # only retain body/pin fields).
                    if isinstance(item, Mapping):
                        converted_component_markers.append(MarkerSnapshot.from_mapping(item))
                    continue
                converted_components.append(component)
            except ValueError as exc:
                errors.append(f"components[{i}]:{exc}")
        converted_markers: list[MarkerSnapshot] = []
        for i, item in enumerate(self.markers or ()):
            try:
                converted_markers.append(_coerce_marker(item))
            except ValueError as exc:
                errors.append(f"markers[{i}]:{exc}")
        converted_ink: list[InkSnapshot] = []
        for i, item in enumerate(self.ink_boxes or ()):
            try:
                converted_ink.append(_coerce_ink(item))
            except ValueError as exc:
                errors.append(f"ink_boxes[{i}]:{exc}")
        converted_markers.extend(converted_component_markers)
        # Connector result ordering is not stable after a move.  Canonicalize
        # snapshots now so finding order and audit hashes are reproducible.
        converted_components.sort(key=lambda c: (c.ref, c.component_type, c.primitive_id))
        converted_markers.sort(key=lambda m: (m.owner_ref, m.pin, m.net.casefold(), m.kind, m.primitive_id, m.x if m.x is not None else float("inf"), m.y if m.y is not None else float("inf")))
        converted_ink.sort(key=lambda i: (i.ref, i.net.casefold(), i.kind, i.primitive_id, i.bbox.text() if isinstance(i.bbox, Rect) else ""))
        object.__setattr__(self, "components", tuple(converted_components))
        object.__setattr__(self, "markers", tuple(converted_markers))
        object.__setattr__(self, "ink_boxes", tuple(converted_ink))
        for attr, label in (("usable_band", "usable_band"), ("titleblock_keepout", "titleblock_keepout")):
            value = getattr(self, attr)
            if value is None or isinstance(value, Rect):
                continue
            try:
                object.__setattr__(self, attr, Rect.from_value(value))
            except ValueError as exc:
                errors.append(f"{label}:{exc}")
                object.__setattr__(self, attr, None)
        object.__setattr__(self, "pin_to_net", dict(self.pin_to_net or {}))
        object.__setattr__(self, "expected_pin_nets", dict(self.expected_pin_nets or {}))
        object.__setattr__(self, "expected_pin_to_net", dict(self.expected_pin_to_net or {}))
        object.__setattr__(self, "expected_nets", dict(self.expected_nets or {}))
        object.__setattr__(self, "tool", str(self.tool or ""))
        object.__setattr__(self, "connector", str(self.connector or ""))
        object.__setattr__(self, "tool_version", str(self.tool_version or ""))
        object.__setattr__(self, "connector_version", str(self.connector_version or ""))
        object.__setattr__(self, "snapshot_version", str(self.snapshot_version or ""))
        object.__setattr__(self, "readback_status", str(self.readback_status or ""))
        object.__setattr__(self, "readback_error", str(self.readback_error or ""))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))
        object.__setattr__(self, "validation_errors", tuple(errors))

    @property
    def body_components(self) -> tuple[ComponentSnapshot, ...]:
        return tuple(c for c in self.components if c.component_type.lower() in {"part", "component", "symbol", ""})

    @property
    def verified_readback(self) -> bool:
        return not self.degraded and self.readback_status.strip().casefold() in _GOOD_READBACK

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LayoutSnapshot":
        # Connector commands usually wrap the payload in ``result`` while
        # callers often add page/metadata beside that wrapper.  Use a shallow
        # overlay so both shapes are accepted, with result fields taking
        # precedence.
        nested = value.get("result")
        result = dict(value)
        if isinstance(nested, Mapping):
            result.update(nested)
        readback = result.get("readback") if isinstance(result.get("readback"), Mapping) else {}
        status = _first(result, "readback_status", "readbackStatus", default=None)
        if status is None:
            status = _first(readback, "status", "state", default="ok")
        degraded = bool(_first(result, "degraded", default=False) or _first(readback, "degraded", default=False))
        return cls(
            page=_first(result, "page", "doc", "document", default="") or "",
            components=_first(result, "components", default=()) or (),
            markers=_first(result, "markers", "markerList", default=()) or (),
            ink_boxes=_first(result, "ink_boxes", "inkBoxes", "ink", default=()) or (),
            usable_band=_first(result, "usable_band", "usableBand", "sheetUsable", default=None),
            titleblock_keepout=_first(result, "titleblock_keepout", "titleblockKeepout", "titleblock", default=None),
            pin_to_net=_first(result, "pin_to_net", "pinToNet", default={}) or {},
            expected_pin_nets=_first(result, "expected_pin_nets", "expectedPinNets", default={}) or {},
            expected_pin_to_net=_first(result, "expected_pin_to_net", "expectedPinToNet", default={}) or {},
            expected_nets=_first(result, "expected_nets", "expectedNets", default={}) or {},
            tool=_first(result, "tool", "eda", default="") or "",
            connector=_first(result, "connector", default="") or "",
            tool_version=_first(result, "tool_version", "toolVersion", default="") or "",
            connector_version=_first(result, "connector_version", "connectorVersion", default="") or "",
            snapshot_version=_first(result, "snapshot_version", "snapshotVersion", default=SNAPSHOT_VERSION) or SNAPSHOT_VERSION,
            readback_status=status,
            degraded=degraded,
            readback_error=_first(result, "readback_error", "readbackError", default="") or "",
            oversize=bool(_first(result, "oversize", "isOversize", default=False)),
            metadata=_first(result, "metadata", default={}) or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshotVersion": self.snapshot_version,
            "page": self.page,
            "components": [c.to_dict() for c in self.components],
            "markers": [m.to_dict() for m in self.markers],
            "inkBoxes": [i.to_dict() for i in self.ink_boxes],
            "usableBand": self.usable_band.to_dict() if isinstance(self.usable_band, Rect) else None,
            "titleblockKeepout": self.titleblock_keepout.to_dict() if isinstance(self.titleblock_keepout, Rect) else None,
            "pinToNet": {str(k): v for k, v in self.pin_to_net.items()},
            "expectedPinNets": {str(k): v for k, v in self.expected_pin_nets.items()},
            "readback": {
                "status": self.readback_status,
                "degraded": self.degraded,
                **({"error": self.readback_error} if self.readback_error else {}),
            },
            "tool": self.tool,
            "connector": self.connector,
            "toolVersion": self.tool_version,
            "connectorVersion": self.connector_version,
            "oversize": self.oversize,
            "metadata": dict(self.metadata),
            **({"validationErrors": list(self.validation_errors)} if self.validation_errors else {}),
        }


@dataclass(frozen=True, slots=True)
class LayoutAudit:
    """Result object for callers that need both findings and serialized data."""

    snapshot: LayoutSnapshot | None
    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        # ``findings`` are all strong errors today.  Keep the explicit
        # readback condition so future warning-only checks cannot accidentally
        # turn an unverified page into PASS.
        return self.snapshot is not None and self.snapshot.verified_readback and not self.findings

    @property
    def passed(self) -> bool:
        return self.ok

    @property
    def verified(self) -> bool:
        return self.snapshot is not None and self.snapshot.verified_readback

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "verified": self.verified,
            "snapshot": self.snapshot.to_dict() if self.snapshot is not None else None,
            "findings": [f.model_dump() for f in self.findings],
        }


def _where(*, ref: str = "", net: str = "", pin: str = "", xy: str = "") -> Where:
    return Where(ref=str(ref or ""), net=str(net or ""), pin=str(pin or ""), xy=str(xy or ""))


def _finding(code: str, *, ref: str = "", net: str = "", pin: str = "", xy: str = "", evidence: str, fix: str) -> Finding:
    return Finding(
        code=code,
        where=_where(ref=ref, net=net, pin=pin, xy=xy),
        evidence=evidence,
        severity="error",
        suggested_fix_class=fix,
        weak=False,
    )


def _fmt_point(x: float, y: float) -> str:
    return f"{x:g},{y:g}"


def _fmt_rect(rect: Rect | None) -> str:
    return rect.text() if isinstance(rect, Rect) else "?"


def _valid_rect(value: Any) -> Rect | None:
    if isinstance(value, Rect) and value.valid:
        return value
    return None


def _net_equal(a: Any, b: Any) -> bool:
    return str(a or "").strip().casefold() == str(b or "").strip().casefold()


def _map_lookup(mapping: Mapping[Any, Any], ref: str, pin: str) -> tuple[bool, Any]:
    """Look up flat (``R1:1``) and nested ({``R1``: {``1``: ...}}) maps."""

    candidates: tuple[Any, ...] = (f"{ref}:{pin}", f"{ref}/{pin}", (ref, pin))
    for key in candidates:
        try:
            if key in mapping:
                return True, mapping[key]
        except TypeError:
            continue
    if ref in mapping and isinstance(mapping[ref], Mapping):
        nested = mapping[ref]
        for key in (pin, str(pin)):
            if key in nested:
                return True, nested[key]
    return False, None


def _actual_pin_net(snapshot: LayoutSnapshot, pin: PinSnapshot) -> str:
    found, value = _map_lookup(snapshot.pin_to_net, pin.ref, pin.pin)
    if found:
        return str(value or "")
    return pin.net


def _expected_pin_net(snapshot: LayoutSnapshot, component: ComponentSnapshot, pin: PinSnapshot) -> tuple[bool, Any]:
    if pin.expected_net is not None:
        return True, pin.expected_net
    # Component-local maps conventionally use just the pin number
    # (``{"1": "VCC"}``); page-level maps need the owner-qualified key to
    # avoid ambiguity between equal pin numbers on different components.
    if pin.pin in component.expected_pin_nets:
        return True, component.expected_pin_nets[pin.pin]
    for mapping in (snapshot.expected_pin_nets, snapshot.expected_pin_to_net, snapshot.expected_nets):
        found, value = _map_lookup(mapping, pin.ref, pin.pin)
        if found:
            return True, value
    return False, None


def check_snapshot_readback(snapshot: LayoutSnapshot | None, *, require_components: bool = True) -> list[Finding]:
    """Validate that a terminal snapshot is present and sufficiently complete."""

    if snapshot is None:
        return [_finding(LAYOUT_READ_UNVERIFIED, evidence="layout snapshot is missing; final geometry was not read back", fix="RETRY_READBACK")]
    findings: list[Finding] = []
    if not snapshot.page.strip():
        findings.append(_finding(LAYOUT_SNAPSHOT_INVALID, evidence="layout snapshot has no page/document identifier", fix="RETRY_READBACK"))
    if not snapshot.verified_readback:
        detail = snapshot.readback_error or f"readback_status={snapshot.readback_status!r}"
        if snapshot.degraded:
            detail += "; degraded=true"
        findings.append(_finding(LAYOUT_READ_UNVERIFIED, ref=snapshot.page, evidence=f"layout readback is not verified: {detail}", fix="RETRY_READBACK"))
    if snapshot.snapshot_version.strip() == "":
        findings.append(_finding(LAYOUT_SNAPSHOT_INVALID, ref=snapshot.page, evidence="snapshot version is empty", fix="RETRY_READBACK"))
    band = _valid_rect(snapshot.usable_band)
    if band is None:
        findings.append(_finding(LAYOUT_SNAPSHOT_INVALID, ref=snapshot.page, evidence="usable sheet band is missing or invalid", fix="RETRY_READBACK"))
    if require_components and not snapshot.components:
        findings.append(_finding(LAYOUT_SNAPSHOT_INVALID, ref=snapshot.page, evidence="component readback is empty; empty/stale output is not a valid final page", fix="RETRY_READBACK"))
    seen_refs: set[str] = set()
    for component in snapshot.components:
        if component.validation_errors:
            findings.append(_finding(LAYOUT_SNAPSHOT_INVALID, ref=component.ref, evidence=f"component snapshot invalid: {'; '.join(component.validation_errors)}", fix="RETRY_READBACK"))
        if not component.ref:
            findings.append(_finding(LAYOUT_SNAPSHOT_INVALID, evidence="component readback contains an empty designator", fix="RETRY_READBACK"))
        elif component.ref in seen_refs:
            findings.append(_finding(LAYOUT_SNAPSHOT_INVALID, ref=component.ref, evidence="component readback contains duplicate designator", fix="RETRY_READBACK"))
        seen_refs.add(component.ref)
        if component.component_type.lower() in {"part", "component", "symbol", ""} and _valid_rect(component.bbox) is None:
            findings.append(_finding(LAYOUT_SNAPSHOT_INVALID, ref=component.ref, evidence="real component body bbox is missing or invalid", fix="RETRY_READBACK"))
        for pin in component.pins:
            if not pin.pin or not isfinite(pin.x) or not isfinite(pin.y):
                findings.append(_finding(LAYOUT_SNAPSHOT_INVALID, ref=component.ref, pin=pin.pin, evidence=f"pin snapshot has invalid identity/coordinates ({pin.x!r},{pin.y!r})", fix="RETRY_READBACK"))
        for marker_error in ():  # keeps marker validation in the loop below
            del marker_error
    for marker in snapshot.markers:
        if marker.validation_errors or (marker.effective_bbox() is None):
            detail = "; ".join(marker.validation_errors) or "marker has no coordinate or ink bbox"
            findings.append(_finding(LAYOUT_SNAPSHOT_INVALID, ref=marker.owner_ref, net=marker.net, pin=marker.pin, evidence=f"marker snapshot invalid: {detail}", fix="RETRY_READBACK"))
    for ink in snapshot.ink_boxes:
        if ink.validation_errors or _valid_rect(ink.bbox) is None:
            detail = "; ".join(ink.validation_errors) or "ink bbox is missing or invalid"
            findings.append(_finding(LAYOUT_SNAPSHOT_INVALID, ref=ink.ref, net=ink.net, evidence=f"ink snapshot invalid: {detail}", fix="RETRY_READBACK"))
    if snapshot.validation_errors:
        findings.append(_finding(LAYOUT_SNAPSHOT_INVALID, ref=snapshot.page, evidence=f"snapshot conversion errors: {'; '.join(snapshot.validation_errors)}", fix="RETRY_READBACK"))
    return findings


def check_body_overlaps(snapshot: LayoutSnapshot | None, *, tolerance: float = 0.0) -> list[Finding]:
    if snapshot is None:
        return []
    parts = [(c.ref, _valid_rect(c.bbox)) for c in snapshot.body_components]
    parts = [(ref, rect) for ref, rect in parts if ref and rect is not None]
    findings: list[Finding] = []
    for i, (ref_a, rect_a) in enumerate(parts):
        for ref_b, rect_b in parts[i + 1 :]:
            assert rect_a is not None and rect_b is not None
            if not rect_a.intersects(rect_b, max(0.0, tolerance)):
                continue
            x = (max(rect_a.min_x, rect_b.min_x) + min(rect_a.max_x, rect_b.max_x)) / 2.0
            y = (max(rect_a.min_y, rect_b.min_y) + min(rect_a.max_y, rect_b.max_y)) / 2.0
            findings.append(_finding(
                LAYOUT_BODY_OVERLAP,
                ref=ref_a,
                xy=_fmt_point(x, y),
                evidence=f"component bodies overlap: {ref_a} {_fmt_rect(rect_a)} with {ref_b} {_fmt_rect(rect_b)}",
                fix="RELAYOUT",
            ))
    return findings


def check_pin_coincidences(snapshot: LayoutSnapshot | None, *, tolerance: float = 0.0) -> list[Finding]:
    if snapshot is None:
        return []
    pins = [p for c in snapshot.components for p in c.pins if p.ref and p.pin and isfinite(p.x) and isfinite(p.y)]
    findings: list[Finding] = []
    tol = max(0.0, float(tolerance))
    for i, a in enumerate(pins):
        for b in pins[i + 1 :]:
            if a.ref == b.ref and a.pin == b.pin:
                continue
            if hypot(a.x - b.x, a.y - b.y) > tol:
                continue
            findings.append(_finding(
                LAYOUT_PIN_COINCIDENCE,
                ref=a.ref,
                pin=a.pin,
                xy=_fmt_point((a.x + b.x) / 2.0, (a.y + b.y) / 2.0),
                evidence=f"pins coincide at {_fmt_point((a.x + b.x) / 2.0, (a.y + b.y) / 2.0)}: {a.key} net={a.net!r}, {b.key} net={b.net!r}",
                fix="RELAYOUT",
            ))
    return findings


def check_duplicate_markers(snapshot: LayoutSnapshot | None) -> list[Finding]:
    if snapshot is None:
        return []
    grouped: dict[tuple[str, str, str], list[MarkerSnapshot]] = {}
    for marker in snapshot.markers:
        # Without an owner pin there is no proof that two global labels are
        # duplicate carriers for one pin; leave that ambiguity to review.
        if marker.owner_ref and marker.pin and marker.net:
            grouped.setdefault((marker.owner_ref, marker.pin, marker.net.casefold()), []).append(marker)
    findings: list[Finding] = []
    for (ref, pin, _net_key), markers in sorted(grouped.items()):
        if len(markers) < 2:
            continue
        net = markers[0].net
        ids = [m.primitive_id or f"{m.kind}@{m.x:g},{m.y:g}" if m.x is not None and m.y is not None else m.kind for m in markers]
        findings.append(_finding(
            LAYOUT_DUPLICATE_MARKER,
            ref=ref,
            pin=pin,
            net=net,
            evidence=f"{len(markers)} marker carriers for the same pin/net {ref}:{pin} -> {net}: {', '.join(ids)}",
            fix="DEDUPE_MARKER",
        ))
    return findings


def check_pin_net_mismatches(snapshot: LayoutSnapshot | None) -> list[Finding]:
    if snapshot is None:
        return []
    findings: list[Finding] = []
    for component in snapshot.components:
        for pin in component.pins:
            has_expected, expected = _expected_pin_net(snapshot, component, pin)
            if not has_expected:
                continue
            actual = _actual_pin_net(snapshot, pin)
            if _net_equal(actual, expected):
                continue
            findings.append(_finding(
                LAYOUT_PIN_NET_MISMATCH,
                ref=pin.ref or component.ref,
                pin=pin.pin,
                net=str(expected or ""),
                evidence=f"pin {pin.ref}:{pin.pin} expected net {str(expected or '')!r}, read back {actual!r}",
                fix="REBIND_NET",
            ))
    return findings


def check_expected_nets(snapshot: LayoutSnapshot | None) -> list[Finding]:
    """Check planned page nets against terminal pin/marker carriers."""
    if snapshot is None or not snapshot.expected_nets:
        return []
    page_value = snapshot.expected_nets.get(snapshot.page)
    source: Any = page_value if page_value is not None else snapshot.expected_nets
    expected: set[str] = set()
    if isinstance(source, Mapping):
        for key, value in source.items():
            if isinstance(value, bool):
                if value:
                    expected.add(str(key).strip())
            elif isinstance(value, (str, bytes)):
                expected.add(str(value).strip())
            elif isinstance(value, Iterable):
                expected.update(str(v).strip() for v in value if str(v).strip())
    elif isinstance(source, Iterable) and not isinstance(source, (str, bytes)):
        expected.update(str(v).strip() for v in source if str(v).strip())
    expected = {n for n in expected if n and n.casefold() != "nc"}
    if not expected:
        return []
    actual: set[str] = set(str(v).strip() for v in snapshot.pin_to_net.values() if str(v).strip())
    for component in snapshot.components:
        actual.update(pin.net.strip() for pin in component.pins if pin.net.strip())
    actual.update(marker.net.strip() for marker in snapshot.markers if marker.net.strip())
    return [
        _finding(
            LAYOUT_NET_MISSING,
            ref=snapshot.page,
            net=net,
            evidence=f"planned net {net} has no terminal carrier in page readback",
            fix="REWIRE",
        )
        for net in sorted(expected - actual, key=str.casefold)
    ]


def _ink_items(snapshot: LayoutSnapshot, *, include_body: bool = True) -> Iterable[tuple[str, str, str, str, Rect]]:
    if include_body:
        for component in snapshot.components:
            rect = _valid_rect(component.bbox)
            if rect is not None:
                yield component.ref, "", "", "body", rect
            rect = _valid_rect(component.ink_bbox)
            if rect is not None:
                yield component.ref, "", "", "component-ink", rect
    for marker in snapshot.markers:
        rect = _valid_rect(marker.effective_bbox())
        if rect is not None:
            yield marker.owner_ref, marker.net, marker.pin, marker.kind, rect
    for ink in snapshot.ink_boxes:
        rect = _valid_rect(ink.bbox)
        if rect is not None:
            yield ink.ref, ink.net, "", ink.kind, rect


def check_ink_bounds(
    snapshot: LayoutSnapshot | None,
    *,
    tolerance: float = 0.0,
    allow_oversize: bool = False,
    include_body: bool = True,
) -> list[Finding]:
    if snapshot is None or not isinstance(snapshot.usable_band, Rect) or not snapshot.usable_band.valid:
        return []
    if allow_oversize and snapshot.oversize:
        return []
    findings: list[Finding] = []
    for ref, net, pin, kind, rect in _ink_items(snapshot, include_body=include_body):
        if not rect.outside(snapshot.usable_band, max(0.0, float(tolerance))):
            continue
        findings.append(_finding(
            LAYOUT_INK_OUT_OF_BAND,
            ref=ref,
            net=net,
            pin=pin,
            xy=rect.text(),
            evidence=f"{kind} ink {_fmt_rect(rect)} is outside usable band {_fmt_rect(snapshot.usable_band)}",
            fix="RELAYOUT",
        ))
    return findings


def check_titleblock_occlusion(snapshot: LayoutSnapshot | None, *, tolerance: float = 0.0, allow_oversize: bool = False) -> list[Finding]:
    if snapshot is None or not isinstance(snapshot.titleblock_keepout, Rect) or not snapshot.titleblock_keepout.valid:
        return []
    if allow_oversize and snapshot.oversize:
        return []
    findings: list[Finding] = []
    for ref, net, pin, kind, rect in _ink_items(snapshot):
        if not rect.intersects(snapshot.titleblock_keepout, max(0.0, float(tolerance))):
            continue
        findings.append(_finding(
            LAYOUT_TITLEBLOCK_OCCLUDE,
            ref=ref,
            net=net,
            pin=pin,
            xy=rect.text(),
            evidence=f"{kind} ink {_fmt_rect(rect)} occludes titleblock keepout {_fmt_rect(snapshot.titleblock_keepout)}",
            fix="RELAYOUT",
        ))
    return findings


def audit_layout_snapshot(
    snapshot: LayoutSnapshot | Mapping[str, Any] | None,
    *,
    body_tolerance: float = 0.0,
    pin_tolerance: float = 0.0,
    ink_tolerance: float = 0.0,
    allow_oversize: bool = False,
    require_components: bool = True,
) -> LayoutAudit:
    """Run all terminal checks in deterministic order and deduplicate findings."""

    if isinstance(snapshot, Mapping):
        snapshot = LayoutSnapshot.from_mapping(snapshot)
    if snapshot is not None and not isinstance(snapshot, LayoutSnapshot):
        snapshot = None
    findings: list[Finding] = []
    findings.extend(check_snapshot_readback(snapshot, require_components=require_components))
    findings.extend(check_body_overlaps(snapshot, tolerance=body_tolerance))
    findings.extend(check_pin_coincidences(snapshot, tolerance=pin_tolerance))
    findings.extend(check_duplicate_markers(snapshot))
    findings.extend(check_pin_net_mismatches(snapshot))
    findings.extend(check_expected_nets(snapshot))
    findings.extend(check_ink_bounds(snapshot, tolerance=ink_tolerance, allow_oversize=allow_oversize))
    findings.extend(check_titleblock_occlusion(snapshot, tolerance=ink_tolerance, allow_oversize=allow_oversize))
    unique: list[Finding] = []
    seen: set[str] = set()
    for finding in findings:
        key = finding.key()
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return LayoutAudit(snapshot=snapshot, findings=tuple(unique))


def check_layout_snapshot(snapshot: LayoutSnapshot | Mapping[str, Any] | None, **kwargs: Any) -> list[Finding]:
    """Convenience list-returning API for existing ``validate`` call sites."""

    return list(audit_layout_snapshot(snapshot, **kwargs).findings)


# Readable aliases for integrations and tests.
validate_layout_snapshot = check_layout_snapshot
terminal_layout_findings = check_layout_snapshot
audit_snapshot = audit_layout_snapshot


__all__ = [
    "Rect",
    "PinSnapshot",
    "ComponentSnapshot",
    "MarkerSnapshot",
    "InkSnapshot",
    "LayoutSnapshot",
    "LayoutAudit",
    "LAYOUT_BODY_OVERLAP",
    "LAYOUT_PIN_COINCIDENCE",
    "LAYOUT_DUPLICATE_MARKER",
    "LAYOUT_PIN_NET_MISMATCH",
    "LAYOUT_INK_OUT_OF_BAND",
    "LAYOUT_TITLEBLOCK_OCCLUDE",
    "LAYOUT_NET_MISSING",
    "LAYOUT_READ_UNVERIFIED",
    "LAYOUT_SNAPSHOT_INVALID",
    "check_snapshot_readback",
    "check_body_overlaps",
    "check_pin_coincidences",
    "check_duplicate_markers",
    "check_pin_net_mismatches",
    "check_expected_nets",
    "check_ink_bounds",
    "check_titleblock_occlusion",
    "audit_layout_snapshot",
    "check_layout_snapshot",
    "validate_layout_snapshot",
    "terminal_layout_findings",
    "audit_snapshot",
]
