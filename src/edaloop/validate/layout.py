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

Electrical and body-geometry findings are strong errors.  Render-only findings
(for example a marker drawn over a body or an under-filled non-final page) are
reported as ``weak`` warnings: they must remain visible in the audit, but they
must not be confused with proof that the circuit is electrically invalid.
Oversize is an explicit exception and never applies to readback or pin/net
checks.
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
LAYOUT_COMPONENT_MISSING = "LAYOUT_COMPONENT_MISSING"
LAYOUT_MARKER_ON_BODY = "LAYOUT_MARKER_ON_BODY"
LAYOUT_PAGE_INK_SPARSE = "LAYOUT_PAGE_INK_SPARSE"
LAYOUT_READ_UNVERIFIED = "LAYOUT_READ_UNVERIFIED"
LAYOUT_SNAPSHOT_INVALID = "LAYOUT_SNAPSHOT_INVALID"
# Machine-readable delivery status for render/readability findings that remain
# intentionally weak while marker geometry is only partially observable.
LAYOUT_REVIEW_REQUIRED = "LAYOUT_REVIEW_REQUIRED"

# A few aliases are useful to integrations that use shorter terminology.
BODY_OVERLAP = LAYOUT_BODY_OVERLAP
PIN_COINCIDENCE = LAYOUT_PIN_COINCIDENCE
DUPLICATE_MARKER = LAYOUT_DUPLICATE_MARKER
PIN_NET_MISMATCH = LAYOUT_PIN_NET_MISMATCH
INK_OUT_OF_BAND = LAYOUT_INK_OUT_OF_BAND
READ_UNVERIFIED = LAYOUT_READ_UNVERIFIED
SNAPSHOT_INVALID = LAYOUT_SNAPSHOT_INVALID
REVIEW_REQUIRED = LAYOUT_REVIEW_REQUIRED
NET_MISSING = LAYOUT_NET_MISSING
COMPONENT_MISSING = LAYOUT_COMPONENT_MISSING
MARKER_ON_BODY = LAYOUT_MARKER_ON_BODY
PAGE_INK_SPARSE = LAYOUT_PAGE_INK_SPARSE

SNAPSHOT_VERSION = "1"
# The connector has emitted all of these spellings over time.  Keep the
# accepted set deliberately small and normalize aliases at the boundary so a
# truthy-but-unknown value can never accidentally become a verified snapshot.
_GOOD_READBACK = {"ok", "verified", "complete", "success", "pass", "passed"}
_TRUE_WORDS = {"1", "true", "yes", "y", "on", "ok", "verified", "complete", "success", "pass", "passed"}
_FALSE_WORDS = {"0", "false", "no", "n", "off", "error", "failed", "fail", "degraded", "unknown", "pending", "invalid", "none", "null"}
_SHEET_COMPONENT_TYPES = {
    "sheet",
    "sheet-symbol",
    "sheet_symbol",
    "sheetsymbol",
    "sheet symbol",
    "sheet-frame",
    "sheet_frame",
    "sheetframe",
    "drawing-sheet",
    "drawing_sheet",
    "page",
    "titleblock",
    "border",
}
_BODY_COMPONENT_TYPES = {"part", "component", "symbol", "device", ""}
_MARKER_COMPONENT_TYPES = {
    "netport", "net-port", "net_port", "netflag", "net-flag", "net_flag",
    "netlabel", "net-label", "net_label", "marker",
}
_KNOWN_COMPONENT_TYPES = _BODY_COMPONENT_TYPES | _MARKER_COMPONENT_TYPES | _SHEET_COMPONENT_TYPES


def _normalize_component_type(value: Any) -> str:
    """Canonicalize component/marker aliases emitted by connector versions."""

    text = str(value or "").strip().casefold().replace("_", "-").replace(" ", "-")
    if text in {"", "part", "component", "symbol", "device"}:
        return "part"
    if text in {"netport", "net-port", "netflag", "net-flag", "netlabel", "net-label", "marker"}:
        return {"net-port": "netport", "net-flag": "netflag", "net-label": "netlabel"}.get(text, text)
    if text in {"sheet", "sheet-symbol", "sheetsymbol", "sheet-frame", "drawing-sheet", "titleblock", "border"}:
        return "sheet"
    return text


def _coerce_bool(value: Any, *, default: bool | None = None) -> bool | None:
    """Parse connector booleans without Python's ``bool('false')`` trap."""

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        return default
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in _TRUE_WORDS:
            return True
        if text in _FALSE_WORDS:
            return False
    return default


def _error_values(value: Any) -> list[str]:
    """Normalize validationErrors from JSON-ish connector payloads."""

    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (bytes, bytearray)):
        text = value.decode(errors="replace")
        return [text] if text else []
    if isinstance(value, Mapping):
        # Preserve useful detail while keeping the public field tuple[str,...].
        return [f"{k}={v}" for k, v in value.items()]
    if isinstance(value, Iterable):
        return [str(item) for item in value if str(item)]
    return [str(value)]


def _append_errors(target: list[str], value: Any) -> None:
    """Append errors in order, avoiding duplicate entries on round trips."""

    for error in _error_values(value):
        if error not in target:
            target.append(error)


def _sequence_values(value: Any, label: str, errors: list[str]) -> tuple[Any, ...]:
    """Coerce connector collection fields without leaking ``TypeError``.

    A malformed API response such as ``components: { ... }`` or
    ``pins: 1`` must produce an invalid snapshot that the gate can explain,
    rather than aborting conversion before an audit record is written.
    """

    if value is None:
        errors.append(f"{label}:expected sequence")
        return ()
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        errors.append(f"{label}:expected sequence")
        return ()
    try:
        return tuple(value)
    except Exception:  # noqa: BLE001 - malformed connector iterables fail closed
        errors.append(f"{label}:expected sequence")
        return ()


def _normalize_pin_identity(owner: Any, pin: Any) -> tuple[str, str]:
    """Return a stable owner/pin pair for ``R1:1`` and split-field forms."""

    owner_text = str(owner or "").strip()
    pin_text = str(pin or "").strip()
    if ":" in pin_text:
        qualified_owner, qualified_pin = pin_text.split(":", 1)
        # An explicit owner is authoritative when the two disagree, but the
        # qualified prefix is still removed so keys never become ``R1:R1:1``.
        if not owner_text:
            owner_text = qualified_owner.strip()
        pin_text = qualified_pin.strip()
    return owner_text, pin_text


def _canonical_pin_key(key: Any) -> str:
    """Canonicalize flat/nested pin-map keys for deterministic JSON output."""

    if isinstance(key, (tuple, list)) and len(key) == 2:
        owner, pin = _normalize_pin_identity(key[0], key[1])
        return f"{owner}:{pin}" if owner else pin
    text = str(key).strip()
    if ":" in text:
        owner, pin = _normalize_pin_identity("", text)
        return f"{owner}:{pin}" if owner else pin
    # ``ref/pin`` is another spelling accepted by _map_lookup.
    if "/" in text:
        owner, pin = text.split("/", 1)
        owner, pin = _normalize_pin_identity(owner, pin)
        return f"{owner}:{pin}" if owner else pin
    return text


def _canonical_json(value: Any, *, sort_sequences: bool = False) -> Any:
    """Convert mappings/sets to deterministic JSON-compatible structures."""

    if isinstance(value, Mapping):
        pairs = sorted(value.items(), key=lambda item: (str(item[0]).casefold(), str(item[0])))
        return {str(key): _canonical_json(item, sort_sequences=sort_sequences) for key, item in pairs}
    if isinstance(value, (set, frozenset)):
        items = [_canonical_json(item, sort_sequences=sort_sequences) for item in value]
        return sorted(items, key=lambda item: (str(item).casefold(), str(item)))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = [_canonical_json(item, sort_sequences=sort_sequences) for item in value]
        if sort_sequences:
            return sorted(items, key=lambda item: (str(item).casefold(), str(item)))
        return items
    return value


def _canonical_map(value: Any, *, pin_keys: bool = False, sort_sequences: bool = False) -> dict[str, Any]:
    """Canonicalize a mapping while tolerating malformed connector fields."""

    if not isinstance(value, Mapping):
        return {}
    pairs = []
    for key, item in value.items():
        canonical_key = _canonical_pin_key(key) if pin_keys else str(key).strip()
        pairs.append((canonical_key, _canonical_json(item, sort_sequences=sort_sequences)))
    pairs.sort(key=lambda item: (item[0].casefold(), item[0]))
    return {key: item for key, item in pairs}


def _canonical_pin_map(
    value: Any,
    *,
    flatten_nested: bool = True,
    errors: list[str] | None = None,
    label: str = "pin map",
) -> dict[str, Any]:
    """Canonicalize a pin-to-net map to owner-qualified flat keys.

    Older adapters used either ``{"U1:1": "VCC"}`` or
    ``{"U1": {"1": "VCC"}}``.  Emitting one shape makes snapshots stable
    across connector versions while ``_map_lookup`` remains backwards
    compatible for callers that construct a snapshot directly.
    """

    if not isinstance(value, Mapping):
        return {}
    flattened: dict[str, Any] = {}
    # Keep the source spelling alongside each canonical key.  Connector
    # payloads may mix ``U1:1``, ``U1/1``, tuple keys, and nested ``U1 -> 1``
    # maps; if two spellings disagree, retaining whichever one happened to
    # arrive last would hide contradictory electrical evidence.
    seen: dict[str, tuple[Any, Any]] = {}

    def _insert(canonical_key: str, canonical_value: Any, source_key: Any) -> None:
        previous = seen.get(canonical_key)
        if previous is not None:
            previous_source, previous_value = previous
            try:
                equal = bool(previous_value == canonical_value)
            except Exception:  # noqa: BLE001 - malformed values fail closed
                equal = False
            if not equal and errors is not None:
                errors.append(
                    f"{label}:canonical pin key collision for {canonical_key!r}: "
                    f"{previous_source!r} conflicts with {source_key!r}"
                )
        seen[canonical_key] = (source_key, canonical_value)
        flattened[canonical_key] = canonical_value

    for key, item in value.items():
        if flatten_nested and isinstance(item, Mapping) and ":" not in str(key) and "/" not in str(key):
            owner = str(key).strip()
            for nested_key, nested_value in item.items():
                pin_key = _canonical_pin_key((owner, nested_key))
                _insert(pin_key, _canonical_json(nested_value), (key, nested_key))
            continue
        _insert(_canonical_pin_key(key), _canonical_json(item), key)
    return {key: flattened[key] for key in sorted(flattened, key=lambda k: (k.casefold(), k))}


def _canonical_expected_nets(value: Any, *, page: str = "") -> dict[str, Any]:
    """Normalize expected-net declarations to ``page -> sorted list``."""

    if value is None:
        return {}
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            page_key = str(key).strip()
            if isinstance(item, Mapping):
                # A map of net -> bool is accepted by check_expected_nets.
                out[page_key] = _canonical_json(item, sort_sequences=True)
            elif isinstance(item, (str, bytes)):
                net = str(item).strip()
                out[page_key] = [net] if net else []
            elif isinstance(item, Iterable):
                values = [str(v).strip() for v in item if str(v).strip()]
                out[page_key] = sorted(set(values), key=lambda n: (n.casefold(), n))
            else:
                out[page_key] = _canonical_json(item)
        return {key: out[key] for key in sorted(out, key=lambda k: (k.casefold(), k))}
    if isinstance(value, (str, bytes)):
        net = str(value).strip()
        return {str(page or ""): ([net] if net else [])}
    if isinstance(value, Iterable):
        values = [str(v).strip() for v in value if str(v).strip()]
        return {str(page or ""): sorted(set(values), key=lambda n: (n.casefold(), n))}
    return {}


def _status_value(value: Any) -> tuple[str, bool]:
    """Normalize one status/ok value; second result says it was explicit."""

    if isinstance(value, bool):
        return ("ok" if value else "error", True)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return "ok", True
        if value == 0:
            return "error", True
        return "unknown", True
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in _GOOD_READBACK:
            return text, True
        if text in _TRUE_WORDS:
            return "ok", True
        if text in _FALSE_WORDS:
            return text, True
        return text or "unknown", True
    return "unknown", value is not None


def _readback_fields(result: Mapping[str, Any]) -> tuple[str, bool, str, list[str]]:
    """Extract status/degraded/error from connector variants, fail-closed."""

    nested_raw = result.get("readback")
    nested = nested_raw if isinstance(nested_raw, Mapping) else {}
    status_candidates: list[tuple[int, Any]] = []
    errors: list[str] = []
    if nested_raw is not None and not isinstance(nested_raw, (Mapping, str, bool, int, float)):
        errors.append("readback:expected object or status")

    # Explicit status has precedence over generic result ``status``/``ok``;
    # retain lower-priority indicators too so contradictory payloads fail
    # closed instead of silently trusting whichever key happened to be read.
    for key in ("readback_status", "readbackStatus"):
        if key in result and result[key] is not None:
            status_candidates.append((0, result[key]))
    if isinstance(nested_raw, (str, bool, int, float)):
        status_candidates.append((1, nested_raw))
    for key in ("status", "state", "readbackStatus", "readback_status"):
        if key in nested and nested[key] is not None:
            status_candidates.append((1, nested[key]))
    for key in ("status", "state"):
        if key in result and result[key] is not None:
            status_candidates.append((2, result[key]))
    for source, priority in ((nested, 3), (result, 4)):
        for key in ("ok", "verified", "success", "passed"):
            if key in source and source[key] is not None:
                status_candidates.append((priority, source[key]))

    if not status_candidates:
        status = "unknown"
    else:
        status_candidates.sort(key=lambda item: item[0])
        parsed = [_status_value(item)[0] for _priority, item in status_candidates]
        positives = [item in _GOOD_READBACK for item in parsed]
        negatives = [item not in _GOOD_READBACK for item in parsed]
        # Contradictory indicators are never considered verified.
        if any(positives) and any(negatives):
            status = "error"
            errors.append("conflicting readback status indicators")
        else:
            status = parsed[0]

    degraded_raw = result.get("degraded")
    if degraded_raw is None and nested:
        degraded_raw = nested.get("degraded")
    if degraded_raw is None:
        degraded = False
    else:
        degraded = _coerce_bool(degraded_raw, default=None)
        if degraded is None:
            degraded = True
            errors.append("degraded flag is malformed")

    readback_error = _first(result, "readback_error", "readbackError", default=None)
    if readback_error is None and nested:
        readback_error = _first(nested, "error", "message", default=None)
    if readback_error is None and status not in _GOOD_READBACK:
        # Keep a concrete reason for audit consumers even when the connector
        # only supplied ``ok: false`` or omitted the status entirely.
        readback_error = "readback status is not verified"
    return status, bool(degraded), str(readback_error or ""), errors


def _payload_has_content(value: Any) -> bool:
    """Return whether a connector error/failure field is meaningful."""

    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (Mapping, Sequence, set, frozenset)):
        return bool(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    return True


def _explicit_failure_reason(
    payload: Any, *, include_status: bool = True, include_verdict: bool = True
) -> str:
    """Find an explicit negative marker in a readback envelope.

    ``errors`` (plural) is intentionally ignored: layout/checker payloads use
    it for finding counts while still carrying valid geometry.  Singular
    transport fields and nested ``ok:false`` markers are hard evidence that
    the snapshot cannot be trusted.
    """

    if not isinstance(payload, Mapping):
        return ""
    for key in ("ok", "success", "passed"):
        if key in payload:
            status, _explicit = _status_value(payload.get(key))
            if status in _FALSE_WORDS or status in {"error", "failed", "fail"}:
                return f"{key}={payload.get(key)!r}"
    for key in ("error", "exception", "failure"):
        if key in payload and _payload_has_content(payload.get(key)):
            return f"{key} payload"
    if include_status and "status" in payload:
        status, _explicit = _status_value(payload.get("status"))
        if status in _FALSE_WORDS or status in {"error", "failed", "fail"}:
            return f"status={payload.get('status')!r}"
    if include_verdict and "verdict" in payload:
        status, _explicit = _status_value(payload.get("verdict"))
        if status in _FALSE_WORDS or status in {"error", "failed", "fail"}:
            return f"verdict={payload.get('verdict')!r}"
    for key in ("result", "data", "detail"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            reason = _explicit_failure_reason(
                nested, include_status=include_status, include_verdict=include_verdict
            )
            if reason:
                return f"{key}.{reason}"
    return ""


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


def _collection_field(mapping: Mapping[str, Any], *keys: str) -> Any:
    """Return a collection field while distinguishing absent from explicit null."""

    return _first(mapping, *keys, default=())


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
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        owner, pin = _normalize_pin_identity(self.ref, self.pin)
        object.__setattr__(self, "ref", owner)
        object.__setattr__(self, "pin", pin)
        object.__setattr__(self, "x", _as_float(self.x))
        object.__setattr__(self, "y", _as_float(self.y))
        object.__setattr__(self, "net", str(self.net or ""))
        if self.expected_net is not None:
            object.__setattr__(self, "expected_net", str(self.expected_net))
        object.__setattr__(self, "primitive_id", str(self.primitive_id or ""))
        errors: list[str] = []
        _append_errors(errors, self.validation_errors)
        object.__setattr__(self, "validation_errors", tuple(errors))

    @property
    def key(self) -> str:
        return f"{self.ref}:{self.pin}"

    @property
    def pin_number(self) -> str:
        return self.pin

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, ref: str = "") -> "PinSnapshot":
        raw_pin = _first(value, "pin", "pinNumber", "pin_number", "number", "name", default="")
        owner = _first(value, "ref", "designator", "ownerRef", default=ref)
        if (not raw_pin) and isinstance(_first(value, "pinRef", "pin_ref", default=""), str):
            raw_pin = _first(value, "pinRef", "pin_ref", default="")
        owner, raw_pin = _normalize_pin_identity(owner, raw_pin)
        return cls(
            ref=owner,
            pin=str(raw_pin or ""),
            x=_first(value, "x", "X", default=float("nan")),
            y=_first(value, "y", "Y", default=float("nan")),
            net=_first(value, "net", "netName", "actualNet", default="") or "",
            expected_net=_first(value, "expected_net", "expectedNet", default=None),
            primitive_id=_first(value, "primitiveId", "primitive_id", "id", default="") or "",
            validation_errors=_first(value, "validation_errors", "validationErrors", default=()) or (),
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
        if self.validation_errors:
            out["validationErrors"] = list(self.validation_errors)
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
        errors: list[str] = []
        _append_errors(errors, self.validation_errors)
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
        for i, pin in enumerate(_sequence_values(self.pins, "pins", errors)):
            if isinstance(pin, PinSnapshot):
                converted.append(
                    pin
                    if pin.ref
                    else PinSnapshot(
                        self.ref,
                        pin.pin,
                        pin.x,
                        pin.y,
                        pin.net,
                        pin.expected_net,
                        pin.primitive_id,
                        pin.validation_errors,
                    )
                )
                continue
            if isinstance(pin, Mapping):
                converted.append(PinSnapshot.from_mapping(pin, ref=self.ref))
                continue
            errors.append(f"pin[{i}]:unsupported value")
        converted.sort(key=lambda p: (p.ref, p.pin, p.net.casefold(), p.x, p.y, p.primitive_id))
        object.__setattr__(self, "pins", tuple(converted))
        object.__setattr__(self, "component_type", _normalize_component_type(self.component_type))
        object.__setattr__(self, "primitive_id", str(self.primitive_id or ""))
        expected = self.expected_pin_nets
        if expected is None:
            expected = {}
        if not isinstance(expected, Mapping):
            errors.append("expected_pin_nets:unsupported value")
            expected = {}
        object.__setattr__(
            self,
            "expected_pin_nets",
            _canonical_map(expected, pin_keys=False),
        )
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
        expected = _first(value, "expected_pin_nets", "expectedPinNets", "pinNets", default={})
        return cls(
            ref=ref,
            bbox=_first(value, "bbox", "body", "box", "body_bbox", "bodyBBox", default=None),
            pins=_collection_field(value, "pins", "pinList"),
            ink_bbox=_first(value, "ink_bbox", "inkBBox", "ink", default=None),
            component_type=_first(value, "component_type", "componentType", "type", default="part") or "part",
            primitive_id=_first(value, "primitive_id", "primitiveId", default="") or "",
            expected_pin_nets={} if expected is None else expected,
            validation_errors=_first(value, "validation_errors", "validationErrors", default=()),
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
            out["expectedPinNets"] = _canonical_map(self.expected_pin_nets)
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
        object.__setattr__(self, "kind", _normalize_component_type(self.kind))
        object.__setattr__(self, "net", str(self.net or ""))
        owner, pin = _normalize_pin_identity(self.owner_ref, self.pin)
        object.__setattr__(self, "owner_ref", owner)
        object.__setattr__(self, "pin", pin)
        object.__setattr__(self, "x", None if self.x is None else _as_float(self.x))
        object.__setattr__(self, "y", None if self.y is None else _as_float(self.y))
        object.__setattr__(self, "primitive_id", str(self.primitive_id or ""))
        errors: list[str] = []
        _append_errors(errors, self.validation_errors)
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
        # Some connector versions expose marker geometry only through its
        # single anchor pin.  Normalize that shape at the boundary so the
        # pure checks can still detect an anchor placed on a body.
        anchor = value.get("pins")
        anchor = anchor[0] if isinstance(anchor, Sequence) and anchor and isinstance(anchor[0], Mapping) else {}
        owner = _first(value, "owner_ref", "ownerRef", "ref", "designator", "owner", default="") or ""
        pin = _first(value, "pin", "pinNumber", "pin_number", "ownerPin", default="") or ""
        pin_ref = _first(value, "pinRef", "pin_ref", default="")
        if not owner and isinstance(anchor, Mapping):
            owner = _first(anchor, "ref", "designator", "ownerRef", "owner", default="") or ""
        if not pin and isinstance(anchor, Mapping):
            pin = _first(anchor, "pin", "pinNumber", "pin_number", "number", "name", default="") or ""
        if (not owner or not pin) and isinstance(pin_ref, str) and ":" in pin_ref:
            owner, pin = pin_ref.split(":", 1)
        owner, pin = _normalize_pin_identity(owner, pin)
        return cls(
            kind=_first(value, "kind", "componentType", "type", default="netport") or "netport",
            net=_first(value, "net", "name", "netName", default="") or "",
            owner_ref=owner,
            pin=pin,
            ink_bbox=_first(value, "ink_bbox", "inkBBox", "bbox", "box", default=None),
            x=_first(value, "x", "X", default=anchor.get("x", anchor.get("X"))),
            y=_first(value, "y", "Y", default=anchor.get("y", anchor.get("Y"))),
            primitive_id=_first(value, "primitive_id", "primitiveId", "id", default="") or "",
            validation_errors=_first(value, "validation_errors", "validationErrors", default=()) or (),
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
        errors: list[str] = []
        _append_errors(errors, self.validation_errors)
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
            validation_errors=_first(value, "validation_errors", "validationErrors", default=()) or (),
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
        errors: list[str] = []
        _append_errors(errors, self.validation_errors)
        object.__setattr__(self, "page", str(self.page or ""))
        converted_components: list[ComponentSnapshot] = []
        converted_component_markers: list[MarkerSnapshot] = []
        for i, item in enumerate(_sequence_values(self.components, "components", errors)):
            try:
                component = _coerce_component(item)
                component_type = _normalize_component_type(component.component_type)
                if component_type in {"netport", "netflag", "netlabel", "marker"}:
                    # ``sch list`` commonly returns markers in the same
                    # components array as parts.  Normalize them here so
                    # callers do not have to know which connector variant was
                    # used.  Marker fields are intentionally read from the
                    # original mapping when available (component snapshots
                    # only retain body/pin fields).
                    if isinstance(item, Mapping):
                        converted_component_markers.append(MarkerSnapshot.from_mapping(item))
                    continue
                # Sheet/page pseudo-components describe the canvas rather than
                # a circuit body.  Keeping them in ``components`` makes an
                # empty/stale page look populated and can create false
                # expected-component matches, so drop them at the boundary.
                if component_type in _SHEET_COMPONENT_TYPES:
                    continue
                if component_type not in _KNOWN_COMPONENT_TYPES:
                    # Unknown records must not make a page look populated and
                    # verified merely because the connector returned a
                    # non-empty array.  Keep the record for audit evidence,
                    # but mark the snapshot invalid so all derived checks are
                    # fail-closed until the schema is explicitly supported.
                    errors.append(
                        f"components[{i}]:unknown component type {component.component_type!r}"
                    )
                converted_components.append(component)
            except ValueError as exc:
                errors.append(f"components[{i}]:{exc}")
        converted_markers: list[MarkerSnapshot] = []
        for i, item in enumerate(_sequence_values(self.markers, "markers", errors)):
            try:
                converted_markers.append(_coerce_marker(item))
            except ValueError as exc:
                errors.append(f"markers[{i}]:{exc}")
        converted_ink: list[InkSnapshot] = []
        for i, item in enumerate(_sequence_values(self.ink_boxes, "ink_boxes", errors)):
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
        # ``pin_to_net`` and the snapshot-level expected map are serialized as
        # flat owner-qualified keys.  Keep component-local expected maps nested
        # by designator/pin because their pin numbers are intentionally local.
        for attr, label in (("pin_to_net", "pin_to_net"), ("expected_pin_to_net", "expected_pin_to_net")):
            value = getattr(self, attr)
            if value is None:
                value = {}
            if not isinstance(value, Mapping):
                errors.append(f"{label}:unsupported value")
                value = {}
            object.__setattr__(
                self,
                attr,
                _canonical_pin_map(value, errors=errors, label=label),
            )

        value = self.expected_pin_nets
        if value is None:
            value = {}
        if not isinstance(value, Mapping):
            errors.append("expected_pin_nets:unsupported value")
            value = {}
        object.__setattr__(self, "expected_pin_nets", _canonical_map(value))

        object.__setattr__(
            self,
            "expected_nets",
            _canonical_expected_nets(self.expected_nets, page=self.page),
        )
        object.__setattr__(self, "tool", str(self.tool or ""))
        object.__setattr__(self, "connector", str(self.connector or ""))
        object.__setattr__(self, "tool_version", str(self.tool_version or ""))
        object.__setattr__(self, "connector_version", str(self.connector_version or ""))
        object.__setattr__(self, "snapshot_version", str(self.snapshot_version or ""))
        status, status_explicit = _status_value(self.readback_status)
        object.__setattr__(self, "readback_status", status if status_explicit else "unknown")
        degraded = _coerce_bool(self.degraded, default=None)
        if degraded is None:
            degraded = True
            errors.append("degraded flag is malformed")
        object.__setattr__(self, "degraded", bool(degraded))
        object.__setattr__(self, "readback_error", str(self.readback_error or ""))
        metadata = self.metadata
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, Mapping):
            errors.append("metadata:unsupported value")
            metadata = {}
        object.__setattr__(self, "metadata", dict(metadata))
        oversize = _coerce_bool(self.oversize, default=None)
        if oversize is None:
            errors.append("oversize flag is malformed")
            oversize = False
        object.__setattr__(self, "oversize", bool(oversize))
        object.__setattr__(self, "validation_errors", tuple(errors))

    @property
    def body_components(self) -> tuple[ComponentSnapshot, ...]:
        return tuple(c for c in self.components if c.component_type.lower() in {"part", "component", "symbol", ""})

    @property
    def verified_readback(self) -> bool:
        if self.degraded or self.readback_status.strip().casefold() not in _GOOD_READBACK:
            return False
        # Conversion errors mean that the connector payload was not fully
        # understood.  A nominal ``ok`` status must not override that evidence.
        if self.validation_errors:
            return False
        if any(component.validation_errors for component in self.components):
            return False
        if any(pin.validation_errors for component in self.components for pin in component.pins):
            return False
        if any(marker.validation_errors for marker in self.markers):
            return False
        if any(ink.validation_errors for ink in self.ink_boxes):
            return False
        return True

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LayoutSnapshot":
        # Connector commands usually wrap the payload in ``result`` while
        # callers often add page/metadata beside that wrapper.  Use a shallow
        # overlay so both shapes are accepted, with result fields taking
        # precedence.  Keep the outer envelope's failure indicators separate:
        # a transport ``ok:false`` must not be overwritten by a stale nested
        # ``result.ok:true`` while a normal outer ``ok:true`` remains a
        # harmless transport acknowledgement.
        nested = value.get("result")
        result = dict(value)
        if isinstance(nested, Mapping):
            result.update(nested)
        status, degraded, readback_error, readback_errors = _readback_fields(result)
        outer_failure = _explicit_failure_reason(
            value, include_status=True, include_verdict=False
        )
        if outer_failure:
            status = "error"
            readback_error = f"outer envelope reports failure: {outer_failure}"
            _append_errors(readback_errors, readback_error)
        nested_readback = result.get("readback") if isinstance(result.get("readback"), Mapping) else {}
        mapping_errors: list[str] = []
        _append_errors(mapping_errors, _first(value, "validation_errors", "validationErrors", default=()))
        if isinstance(nested, Mapping):
            _append_errors(mapping_errors, _first(nested, "validation_errors", "validationErrors", default=()))
        _append_errors(mapping_errors, _first(nested_readback, "validation_errors", "validationErrors", default=()))
        _append_errors(mapping_errors, readback_errors)
        return cls(
            page=_first(result, "page", "doc", "document", default="") or "",
            components=_collection_field(result, "components"),
            markers=_collection_field(result, "markers", "markerList"),
            ink_boxes=_collection_field(result, "ink_boxes", "inkBoxes", "ink"),
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
            readback_error=readback_error,
            oversize=_first(result, "oversize", "isOversize", default=False),
            metadata=_first(result, "metadata", default={}) or {},
            validation_errors=mapping_errors,
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
            "pinToNet": _canonical_pin_map(self.pin_to_net),
            "expectedPinNets": _canonical_map(self.expected_pin_nets),
            "expectedPinToNet": _canonical_pin_map(self.expected_pin_to_net),
            "expectedNets": _canonical_expected_nets(self.expected_nets, page=self.page),
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
        # Weak findings are deliberately visible but do not block the
        # electrical/geometry contract.  Readback verification remains an
        # independent hard requirement, so a warning cannot hide a stale page.
        return (
            self.snapshot is not None
            and self.snapshot.verified_readback
            and not any(not finding.weak for finding in self.findings)
        )

    @property
    def blocking_findings(self) -> tuple[Finding, ...]:
        """Findings that make the terminal audit fail."""

        return tuple(finding for finding in self.findings if not finding.weak)

    @property
    def passed(self) -> bool:
        return self.ok

    @property
    def review_required(self) -> bool:
        """Whether a human visual review is required despite a non-blocking audit.

        Marker/body findings are readability signals, not proof of an
        electrical defect.  Keep them weak for backwards compatibility, but
        expose an explicit delivery status so callers do not mistake
        ``ok=True`` for a fully human-readable drawing.
        """

        return any(
            finding.code == LAYOUT_MARKER_ON_BODY
            for finding in self.findings
        )

    @property
    def review_code(self) -> str:
        return LAYOUT_REVIEW_REQUIRED if self.review_required else ""

    @property
    def verified(self) -> bool:
        return self.snapshot is not None and self.snapshot.verified_readback

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "verified": self.verified,
            "reviewRequired": self.review_required,
            **({"reviewCode": self.review_code} if self.review_required else {}),
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


def _weak_finding(
    code: str,
    *,
    ref: str = "",
    net: str = "",
    pin: str = "",
    xy: str = "",
    evidence: str,
    fix: str,
) -> Finding:
    """Create a render/readability warning without weakening hard checks.

    ``Finding`` deliberately carries both ``severity`` and ``weak``.  Keep
    those values coupled here so a future caller cannot accidentally emit a
    warning that the loop treats as a blocking error (or an error that gets
    hidden as a warning).
    """

    return Finding(
        code=code,
        where=_where(ref=ref, net=net, pin=pin, xy=xy),
        evidence=evidence,
        severity="warn",
        suggested_fix_class=fix,
        weak=True,
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
            if pin.validation_errors:
                findings.append(_finding(
                    LAYOUT_SNAPSHOT_INVALID,
                    ref=pin.ref or component.ref,
                    pin=pin.pin,
                    evidence=f"pin snapshot invalid: {'; '.join(pin.validation_errors)}",
                    fix="RETRY_READBACK",
                ))
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


def _derived_checks_ready(snapshot: LayoutSnapshot | None, *, require_components: bool = True) -> bool:
    """Return whether geometry/electrical findings can trust this snapshot.

    A connector may return stale components together with a failed status, or
    a nominally successful status with malformed collection/geometry fields.
    In either case, running overlap/net/ink predicates would attribute faults
    to data that was never proven to be a terminal readback.  Keep the
    conversion/structural findings from :func:`check_snapshot_readback`, and
    gate all derived checks behind this predicate.
    """

    if snapshot is None or not snapshot.verified_readback:
        return False
    if snapshot.validation_errors:
        return False
    if require_components and not snapshot.components:
        return False
    for component in snapshot.components:
        if component.validation_errors:
            return False
        if component.component_type.casefold() in {"part", "component", "symbol", ""}:
            if _valid_rect(component.bbox) is None:
                return False
            if any(
                pin.validation_errors or not pin.pin or not isfinite(pin.x) or not isfinite(pin.y)
                for pin in component.pins
            ):
                return False
    for marker in snapshot.markers:
        if marker.validation_errors or marker.effective_bbox() is None:
            return False
    for ink in snapshot.ink_boxes:
        if ink.validation_errors or _valid_rect(ink.bbox) is None:
            return False
    return True


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


def check_expected_components(
    snapshot: LayoutSnapshot | None,
    expected_components: Iterable[str] | None,
) -> list[Finding]:
    """Require every designator claimed by apply to survive final readback."""

    # A failed/degraded readback cannot establish which parts are absent;
    # report the stronger readback finding instead of manufacturing one
    # missing-component finding per planned designator.
    if snapshot is None or not expected_components or not snapshot.verified_readback:
        return []
    expected = {str(ref).strip() for ref in expected_components if str(ref).strip()}
    actual = {component.ref for component in snapshot.body_components if component.ref}
    return [
        _finding(
            LAYOUT_COMPONENT_MISSING,
            ref=ref,
            evidence=f"expected component {ref} is missing from terminal page {snapshot.page} readback",
            fix="RETRY_READBACK",
        )
        for ref in sorted(expected - actual, key=str.casefold)
    ]


def _clip_rect(rect: Rect, container: Rect) -> Rect | None:
    """Return the positive-area intersection of two valid rectangles."""

    if not rect.valid or not container.valid:
        return None
    clipped = Rect(
        max(rect.min_x, container.min_x),
        max(rect.min_y, container.min_y),
        min(rect.max_x, container.max_x),
        min(rect.max_y, container.max_y),
    )
    return clipped if clipped.width > 0 and clipped.height > 0 else None


def _union_area(rects: Iterable[Rect]) -> float:
    """Compute the exact area of an axis-aligned rectangle union.

    Layout pages contain tens to a few hundred primitives, so a coordinate
    sweep is both deterministic and inexpensive.  Summing rectangle areas is
    not sufficient here because a cluster ``box`` already includes its marker
    and wire ink; double counting would make a sparse page look denser than it
    is.
    """

    valid = [r for r in rects if r.valid and r.width > 0 and r.height > 0]
    if not valid:
        return 0.0
    x_edges = sorted({x for r in valid for x in (r.min_x, r.max_x)})
    area = 0.0
    for left, right in zip(x_edges, x_edges[1:]):
        width = right - left
        if width <= 0:
            continue
        mid = (left + right) / 2.0
        intervals = sorted(
            (r.min_y, r.max_y)
            for r in valid
            if r.min_x < mid < r.max_x
        )
        if not intervals:
            continue
        covered = 0.0
        lo, hi = intervals[0]
        for next_lo, next_hi in intervals[1:]:
            if next_lo > hi:
                covered += max(0.0, hi - lo)
                lo, hi = next_lo, next_hi
            else:
                hi = max(hi, next_hi)
        covered += max(0.0, hi - lo)
        area += width * covered
    return area


def page_ink_metrics(snapshot: LayoutSnapshot | None) -> dict[str, Any]:
    """Return deterministic page occupancy metrics for audit/UI consumers.

    The metric is deliberately based on *rendered* ink (body, cluster volume,
    marker and wire boxes), clipped to ``sheetUsable``.  It is a readability
    signal, not an electrical proof; callers should still inspect the strong
    geometry findings separately.
    """

    empty: dict[str, Any] = {
        "usableArea": 0.0,
        "inkArea": 0.0,
        "occupancy": None,
        "waste": None,
        "rectCount": 0,
        "inkBBox": None,
    }
    if snapshot is None:
        return empty
    band = _valid_rect(snapshot.usable_band)
    if band is None or band.area <= 0:
        return empty
    clipped: list[Rect] = []
    for _ref, _net, _pin, _kind, rect in _ink_items(snapshot):
        piece = _clip_rect(rect, band)
        if piece is not None:
            clipped.append(piece)
    ink_area = _union_area(clipped)
    occupancy = min(1.0, max(0.0, ink_area / band.area))
    ink_bbox: dict[str, float] | None = None
    if clipped:
        ink_bbox = Rect(
            min(r.min_x for r in clipped),
            min(r.min_y for r in clipped),
            max(r.max_x for r in clipped),
            max(r.max_y for r in clipped),
        ).to_dict()
    return {
        "usableArea": band.area,
        "inkArea": ink_area,
        "occupancy": occupancy,
        "waste": max(0.0, 1.0 - occupancy),
        "rectCount": len(clipped),
        "inkBBox": ink_bbox,
    }


def check_marker_body_overlaps(
    snapshot: LayoutSnapshot | None,
    *,
    tolerance: float = 0.0,
) -> list[Finding]:
    """Warn when rendered marker ink sits on a component body.

    EasyEDA's ordinary layout lint intentionally ignores non-part primitives,
    so a netflag/netport can be electrically connected and still be painted
    over a symbol.  This check stays weak until the connector exposes a
    complete text/font geometry model; it nevertheless gives the planner and
    human reviewer a precise marker/body pair to fix.
    """

    if snapshot is None:
        return []
    parts = [
        (component.ref, _valid_rect(component.bbox))
        for component in snapshot.body_components
        if component.ref
    ]
    parts = [(ref, rect) for ref, rect in parts if rect is not None]
    findings: list[Finding] = []
    for marker in snapshot.markers:
        marker_rect = _valid_rect(marker.effective_bbox())
        if marker_rect is None:
            continue
        for body_ref, body_rect in parts:
            assert body_rect is not None
            # A marker's own anchor is expected to lie on its host pin/body
            # boundary.  Flag/port ink that extends over that host symbol is
            # the readability defect; excluding only the zero-area anchor
            # would otherwise suppress useful bbox-based evidence.
            if not marker_rect.intersects(body_rect, max(0.0, tolerance)):
                continue
            owner = marker.owner_ref or body_ref
            findings.append(
                _weak_finding(
                    LAYOUT_MARKER_ON_BODY,
                    ref=owner,
                    net=marker.net,
                    pin=marker.pin,
                    xy=marker_rect.text(),
                    evidence=(
                        f"marker {marker.kind} {marker.net or '-'}"
                        f" ({marker.pin_ref or 'unowned'}) ink {_fmt_rect(marker_rect)}"
                        f" intersects component body {body_ref} {_fmt_rect(body_rect)}"
                    ),
                    fix="RESEAT_MARKER",
                )
            )
    return findings


# Compatibility spellings used by early layout-audit callers.
check_marker_on_body = check_marker_body_overlaps
check_marker_body_overlap = check_marker_body_overlaps


def check_page_ink_sparse(
    snapshot: LayoutSnapshot | None,
    *,
    min_occupancy: float = 0.20,
    threshold: float | None = None,
    is_last_page: bool = True,
    page_is_last: bool | None = None,
    allow_oversize: bool = False,
) -> list[Finding]:
    """Warn about a non-final page whose rendered ink occupies too little area.

    The final page is intentionally exempt: a small design or a remainder
    page is not automatically a packing bug.  The controller passes the page
    position explicitly when it has a complete plan; pure callers can pass
    ``is_last_page=False`` to opt into the same signal.
    """

    if page_is_last is not None:
        is_last_page = bool(page_is_last)
    if threshold is not None:
        min_occupancy = threshold
    try:
        threshold_value = float(min_occupancy)
    except (TypeError, ValueError):
        threshold_value = 0.20
    if not isfinite(threshold_value):
        threshold_value = 0.20
    threshold_value = min(1.0, max(0.0, threshold_value))
    if (
        snapshot is None
        or is_last_page
        or (snapshot.oversize and allow_oversize)
        or not snapshot.verified_readback
        or not snapshot.body_components
    ):
        return []
    metrics = page_ink_metrics(snapshot)
    occupancy = metrics.get("occupancy")
    if occupancy is None or occupancy >= threshold_value:
        return []
    return [
        _weak_finding(
            LAYOUT_PAGE_INK_SPARSE,
            ref=snapshot.page,
            xy=f"occupancy={float(occupancy):.4f}",
            evidence=(
                f"page {snapshot.page} rendered ink occupancy {float(occupancy):.1%}"
                f" is below {threshold_value:.1%}"
                f" (inkArea={float(metrics['inkArea']):g},"
                f" usableArea={float(metrics['usableArea']):g});"
                " merge/repack related modules before tightening spacing"
            ),
            fix="REPACK",
        )
    ]


check_page_sparse = check_page_ink_sparse


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
    expected_components: Iterable[str] | None = None,
    marker_tolerance: float = 0.0,
    min_ink_occupancy: float = 0.20,
    sparse_threshold: float | None = None,
    is_last_page: bool = True,
    page_is_last: bool | None = None,
    check_sparse: bool = True,
) -> LayoutAudit:
    """Run all terminal checks in deterministic order and deduplicate findings."""

    if isinstance(snapshot, Mapping):
        snapshot = LayoutSnapshot.from_mapping(snapshot)
    if snapshot is not None and not isinstance(snapshot, LayoutSnapshot):
        snapshot = None
    findings: list[Finding] = []
    findings.extend(check_snapshot_readback(snapshot, require_components=require_components))
    # Only derive overlap/net/ink findings from a complete, verified terminal
    # snapshot.  This prevents stale or partial connector output from being
    # misreported as a real design defect; structural conversion findings above
    # remain available to explain why the page was rejected.
    if _derived_checks_ready(snapshot, require_components=require_components):
        findings.extend(check_expected_components(snapshot, expected_components))
        findings.extend(check_body_overlaps(snapshot, tolerance=body_tolerance))
        findings.extend(check_pin_coincidences(snapshot, tolerance=pin_tolerance))
        findings.extend(check_duplicate_markers(snapshot))
        findings.extend(check_pin_net_mismatches(snapshot))
        findings.extend(check_expected_nets(snapshot))
        findings.extend(check_marker_body_overlaps(snapshot, tolerance=marker_tolerance))
        if page_is_last is not None:
            is_last_page = bool(page_is_last)
        findings.extend(
            check_page_ink_sparse(
                snapshot,
                min_occupancy=min_ink_occupancy,
                threshold=sparse_threshold,
                is_last_page=is_last_page,
                allow_oversize=allow_oversize,
            )
            if check_sparse
            else []
        )
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
    "LAYOUT_COMPONENT_MISSING",
    "LAYOUT_MARKER_ON_BODY",
    "LAYOUT_PAGE_INK_SPARSE",
    "LAYOUT_READ_UNVERIFIED",
    "LAYOUT_SNAPSHOT_INVALID",
    "LAYOUT_REVIEW_REQUIRED",
    "MARKER_ON_BODY",
    "PAGE_INK_SPARSE",
    "REVIEW_REQUIRED",
    "check_snapshot_readback",
    "check_body_overlaps",
    "check_pin_coincidences",
    "check_duplicate_markers",
    "check_pin_net_mismatches",
    "check_expected_nets",
    "check_expected_components",
    "check_marker_body_overlaps",
    "check_marker_on_body",
    "check_marker_body_overlap",
    "page_ink_metrics",
    "check_page_ink_sparse",
    "check_page_sparse",
    "check_ink_bounds",
    "check_titleblock_occlusion",
    "audit_layout_snapshot",
    "check_layout_snapshot",
    "validate_layout_snapshot",
    "terminal_layout_findings",
    "audit_snapshot",
]
