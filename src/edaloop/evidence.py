"""Read-only EasyEDA L0 evidence collection.

The collector is intentionally boring: it runs a fixed, read-only command
matrix, stores the exact process evidence, and derives a small amount of
geometry metadata without sending any write action to EasyEDA.  It is meant
for layout investigations and regression baselines, not for changing a
project or deciding that a schematic is production-ready.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


CommandRunner = Callable[[list[str]], tuple[int | None, str, str]]


class ReadOnlyViolation(RuntimeError):
    """Raised before execution when a probe is outside the read-only allowlist."""


# Keep this list explicit.  In particular, do not broaden ``sch`` to a generic
# prefix: EasyEDA has many mutating subcommands (place, modify, clear, save,
# page-new, ...), and a typo here would turn a diagnostic command into a write.
READ_ONLY_COMMANDS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("daemon", "health"),
        ("health",),
        ("version",),
        ("sch", "pages"),
        ("sch", "list"),
        ("sch", "clusters"),
        ("sch", "layout-lint"),
        ("sch", "check"),
        ("sch", "bridge-check"),
        ("sch", "drc"),
        ("sch", "gate"),
        ("sch", "read"),
        ("sch", "nets"),
        ("sch", "netlist"),
        ("sch", "sheet-geometry"),
        ("sch", "text-list"),
        ("sch", "layout-score"),
    }
)

# These options are either known write switches or commonly confused with a
# read option.  Rejecting them is defense in depth in addition to the command
# allowlist above.  ``--force-stale-read`` is deliberately not rejected: it is
# a read-only PCB escape hatch and is not emitted by this collector anyway.
_MUTATING_OPTIONS = frozenset(
    {
        "--apply",
        "--clear",
        "--delete",
        "--save",
        "--write",
        "--mutate",
        "--force",
    }
)


def is_read_only_argv(argv: Sequence[str]) -> bool:
    """Return whether *argv* is one of the collector's safe probe shapes.

    ``argv`` excludes the executable name and may contain global EasyEDA
    options (``--doc``, ``--project``, ``--window``) after the subcommand.
    Unknown options are allowed because the upstream CLI can add harmless
    diagnostic switches without requiring a collector release; write-shaped
    options are always rejected.
    """

    tokens = [str(x) for x in argv]
    if not tokens:
        return False
    # Find the command prefix.  Our generated probes put it first, while this
    # small scan also makes the guard useful to callers that prepend a global
    # flag in a custom runner.
    start = 0
    while start < len(tokens) and tokens[start].startswith("-"):
        start += 1
        # Global options with values (e.g. --project foo) are skipped here.
        if start < len(tokens) and not tokens[start].startswith("-"):
            start += 1
    if start >= len(tokens):
        return False
    prefix = tuple(tokens[start : start + 2])
    if prefix not in READ_ONLY_COMMANDS:
        prefix = (tokens[start],)
        if prefix not in READ_ONLY_COMMANDS:
            return False
    return not any(
        token in _MUTATING_OPTIONS
        or any(token.startswith(option + "=") for option in _MUTATING_OPTIONS)
        for token in tokens
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _json_default(value: Any) -> str:
    return str(value)


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Write JSON through a same-directory temporary file and atomic replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, default=_json_default)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Path.replace maps to an atomic rename on the supported platforms
        # when source and destination share a directory.
        tmp.replace(path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _normalise_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def parse_json_output(text: str) -> tuple[Any | None, str | None, str | None]:
    """Parse a JSON response, tolerating CLI diagnostics before/after JSON.

    ``easyeda daemon health`` and ``sch netlist`` currently print a human
    diagnostic line before their JSON envelope.  The full stdout remains in
    the evidence record; this function only extracts the largest valid JSON
    span for structured summaries.

    Returns ``(payload, raw_json_text, error)``.  ``payload`` and
    ``raw_json_text`` are ``None`` when no JSON object/array can be found.
    """

    source = _normalise_output(text)
    stripped = source.strip()
    if not stripped:
        return None, None, None
    try:
        return json.loads(stripped), stripped, None
    except json.JSONDecodeError as direct_error:
        decoder = json.JSONDecoder()
        candidates: list[tuple[int, int, Any]] = []
        for index, char in enumerate(source):
            if char not in "[{":
                continue
            try:
                payload, end = decoder.raw_decode(source[index:])
            except json.JSONDecodeError:
                continue
            # Prefer the candidate consuming the most output.  This selects
            # the outer response envelope over nested objects in diagnostics.
            candidates.append((index + end, index, payload))
        if candidates:
            end, start, payload = max(candidates, key=lambda item: (item[0], -item[1]))
            return payload, source[start:end], None
        return None, None, f"{type(direct_error).__name__}: {direct_error}"


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, Mapping) and isinstance(payload.get("result"), Mapping):
        return payload["result"]
    return payload


def _walk(value: Any) -> Iterable[Any]:
    """Yield all nested values without assuming a particular response schema."""

    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk(child)


def _first_mapping_list(payload: Any, keys: Sequence[str]) -> list[Mapping[str, Any]]:
    wanted = {key.lower() for key in keys}
    candidates: list[list[Mapping[str, Any]]] = []
    for node in _walk(payload):
        if not isinstance(node, Mapping):
            continue
        for key, child in node.items():
            if str(key).lower() not in wanted or not isinstance(child, list):
                continue
            records = [item for item in child if isinstance(item, Mapping)]
            if records:
                candidates.append(records)
    if not candidates:
        return []
    # The largest list is generally the actual response collection, rather
    # than a small nested ``artifacts``/``pins`` list.
    return max(candidates, key=len)


def _extract_pages(payload: Any) -> list[dict[str, Any]]:
    root = _unwrap(payload)
    candidates: list[Mapping[str, Any]] = []
    if isinstance(root, Mapping):
        direct = root.get("pages")
        if isinstance(direct, list):
            candidates = [item for item in direct if isinstance(item, Mapping)]
            # A few older daemon builds returned a compact string list.
            candidates.extend({"name": item} for item in direct if isinstance(item, str))
    if not candidates:
        # ``pages`` may be nested under schematics[].page in older CLI builds.
        for node in _walk(root):
            if not isinstance(node, Mapping):
                continue
            nested = node.get("page")
            if isinstance(nested, list):
                candidates.extend(item for item in nested if isinstance(item, Mapping))
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        name = str(
            item.get("name")
            or item.get("pageName")
            or item.get("title")
            or ""
        ).strip()
        page_uuid = str(
            item.get("uuid")
            or item.get("pageUuid")
            or item.get("id")
            or ""
        ).strip()
        ref = name or page_uuid
        if not ref:
            continue
        key = page_uuid or name.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "name": name or page_uuid,
                "uuid": page_uuid,
                "ref": ref,
                "parentSchematicUuid": item.get("parentSchematicUuid") or item.get("schematicUuid", ""),
            }
        )
    return result


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, Mapping):
        return None
    source: Mapping[str, Any] = value
    nested = value.get("bbox") or value.get("body")
    if isinstance(nested, Mapping):
        source = nested
    lowered = {str(key).lower(): child for key, child in source.items()}
    vals = [_number(lowered.get(key)) for key in ("minx", "miny", "maxx", "maxy")]
    if any(item is None for item in vals):
        return None
    min_x, min_y, max_x, max_y = (float(item) for item in vals if item is not None)
    if max_x < min_x or max_y < min_y:
        return None
    return min_x, min_y, max_x, max_y


def _extract_components(payload: Any) -> list[Mapping[str, Any]]:
    root = _unwrap(payload)
    if isinstance(root, list):
        return [item for item in root if isinstance(item, Mapping)]
    return _first_mapping_list(root, ("components", "items", "parts"))


def _payload_success(payload: Any) -> bool | None:
    """Extract an explicit transport/application success flag when present."""

    if not isinstance(payload, Mapping):
        return None
    for key in ("ok", "success", "passed"):
        value = payload.get(key)
        if isinstance(value, bool):
            return value
    nested = payload.get("result")
    if isinstance(nested, Mapping):
        for key in ("ok", "success", "passed"):
            value = nested.get(key)
            if isinstance(value, bool):
                return value
    return None


def _component_type(component: Mapping[str, Any]) -> str:
    return str(
        component.get("componentType")
        or component.get("component_type")
        or component.get("type")
        or component.get("kind")
        or ""
    ).strip().casefold()


def _component_ref(component: Mapping[str, Any]) -> str:
    return str(
        component.get("designator")
        or component.get("ref")
        or component.get("reference")
        or component.get("primitiveId")
        or component.get("id")
        or "?"
    ).strip()


def _extract_sheet_usable(*payloads: Any) -> tuple[tuple[float, float, float, float] | None, str]:
    for payload in payloads:
        for node in _walk(payload):
            if not isinstance(node, Mapping):
                continue
            for key, child in node.items():
                if str(key).casefold() in {"sheetusable", "usableband", "sheetusablebbox"}:
                    rect = _bbox(child)
                    if rect:
                        return rect, str(key)
    return None, ""


def _extract_sheet_geometry(
    *payloads: Any,
) -> tuple[
    tuple[float, float, float, float] | None,
    tuple[float, float, float, float] | None,
    str,
]:
    """Extract sheet/title-block geometry from ``sch sheet-geometry`` output."""

    sheet: tuple[float, float, float, float] | None = None
    title_block: tuple[float, float, float, float] | None = None
    source = ""
    for payload in payloads:
        for node in _walk(payload):
            if not isinstance(node, Mapping):
                continue
            candidate = node.get("sheet")
            if isinstance(candidate, Mapping):
                rect = _bbox(candidate.get("bbox") or candidate)
                if rect and sheet is None:
                    sheet = rect
                source = source or str(candidate.get("template") or "sheet-geometry")
            for key in ("titleBlock", "titleblock", "title_block"):
                candidate = node.get(key)
                if isinstance(candidate, Mapping):
                    rect = _bbox(candidate.get("bbox") or candidate)
                    if rect and title_block is None:
                        title_block = rect
                    source = source or str(candidate.get("source") or "sheet-geometry")
            keepouts = node.get("keepouts")
            if isinstance(keepouts, list):
                for keepout in keepouts:
                    if not isinstance(keepout, Mapping):
                        continue
                    name = str(keepout.get("name") or "").casefold()
                    if "title" in name and title_block is None:
                        rect = _bbox(keepout.get("bbox") or keepout)
                        if rect:
                            title_block = rect
    return sheet, title_block, source


def _rect_area(rect: tuple[float, float, float, float]) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _rect_intersection(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    rect = (max(first[0], second[0]), max(first[1], second[1]), min(first[2], second[2]), min(first[3], second[3]))
    return rect if rect[2] > rect[0] and rect[3] > rect[1] else None


def _union_area(rects: Sequence[tuple[float, float, float, float]]) -> float:
    """Exact union area for a small set of axis-aligned rectangles."""

    if not rects:
        return 0.0
    xs = sorted({edge for rect in rects for edge in (rect[0], rect[2])})
    area = 0.0
    for left, right in zip(xs, xs[1:]):
        if right <= left:
            continue
        intervals = [(rect[1], rect[3]) for rect in rects if rect[0] < right and rect[2] > left]
        intervals.sort()
        covered = 0.0
        current: tuple[float, float] | None = None
        for low, high in intervals:
            if current is None:
                current = (low, high)
            elif low > current[1]:
                covered += current[1] - current[0]
                current = (low, high)
            else:
                current = (current[0], max(current[1], high))
        if current is not None:
            covered += current[1] - current[0]
        area += (right - left) * covered
    return area


def _cluster_records(payload: Any) -> list[Mapping[str, Any]]:
    root = _unwrap(payload)
    if isinstance(root, Mapping) and isinstance(root.get("clusters"), list):
        return [item for item in root["clusters"] if isinstance(item, Mapping)]
    return _first_mapping_list(root, ("clusters",))


def _finding_records(payload: Any) -> list[Mapping[str, Any]]:
    return _first_mapping_list(_unwrap(payload), ("findings",))


def derive_layout_summary(
    list_payload: Any = None,
    clusters_payload: Any = None,
    sheet_geometry_payload: Any = None,
) -> dict[str, Any]:
    """Derive conservative overlap and blank-space metrics from read output.

    Body overlaps use ``sch list --include-bbox`` part bboxes.  Cluster boxes
    are reported separately because they include marker/wire ink and therefore
    answer a different question.  No metric is emitted as a PASS verdict.
    """

    components = _extract_components(list_payload)
    parts: list[tuple[str, tuple[float, float, float, float]]] = []
    sheet: tuple[float, float, float, float] | None = None
    for component in components:
        rect = _bbox(component)
        if rect is None:
            continue
        kind = _component_type(component)
        if kind in {"sheet", "drawing-sheet", "page", "frame"}:
            sheet = sheet or rect
            continue
        # Missing componentType is common in small test fixtures and in old
        # connector responses; treat such records as parts when they carry a
        # designator and bbox.
        if kind and kind not in {"part", "component", "device", "symbol"}:
            continue
        parts.append((_component_ref(component), rect))

    clusters = _cluster_records(clusters_payload)
    cluster_boxes: list[tuple[str, tuple[float, float, float, float]]] = []
    for cluster in clusters:
        rect = _bbox(cluster.get("box") if isinstance(cluster, Mapping) else None)
        if rect:
            cluster_boxes.append((_component_ref(cluster), rect))

    geometry_sheet, title_block, geometry_source = _extract_sheet_geometry(sheet_geometry_payload)
    usable, usable_source = _extract_sheet_usable(
        list_payload, clusters_payload, sheet_geometry_payload
    )
    if usable is None:
        usable = geometry_sheet or sheet
        usable_source = geometry_source or ("sheet-bbox" if sheet else "")
    if usable is None and parts:
        min_x = min(rect[0] for _, rect in parts)
        min_y = min(rect[1] for _, rect in parts)
        max_x = max(rect[2] for _, rect in parts)
        max_y = max(rect[3] for _, rect in parts)
        usable = (min_x, min_y, max_x, max_y)
        usable_source = "parts-bbox-fallback"

    overlaps: list[dict[str, Any]] = []
    for index, (ref_a, rect_a) in enumerate(parts):
        for ref_b, rect_b in parts[index + 1 :]:
            intersection = _rect_intersection(rect_a, rect_b)
            if intersection:
                overlaps.append(
                    {
                        "a": ref_a,
                        "b": ref_b,
                        "area": _rect_area(intersection),
                        "bbox": {
                            "minX": intersection[0],
                            "minY": intersection[1],
                            "maxX": intersection[2],
                            "maxY": intersection[3],
                        },
                    }
                )

    # Cluster boxes include a component body plus its marker/wire envelope.
    # Two envelopes can legitimately intersect inside one functional module
    # even when the authoritative ``sch clusters --strict`` check is green.
    # Preserve those raw intersections as diagnostics, but only promote an
    # overlap to the page summary when the checker emits a finding.
    cluster_envelope_overlaps: list[dict[str, Any]] = []
    for index, (ref_a, rect_a) in enumerate(cluster_boxes):
        for ref_b, rect_b in cluster_boxes[index + 1 :]:
            intersection = _rect_intersection(rect_a, rect_b)
            if intersection:
                cluster_envelope_overlaps.append(
                    {"a": ref_a, "b": ref_b, "area": _rect_area(intersection)}
                )
    cluster_overlaps: list[dict[str, Any]] = []
    for finding in _finding_records(clusters_payload):
        kind = str(finding.get("type") or finding.get("kind") or "").casefold()
        if "overlap" in kind:
            pair = {
                "a": str(finding.get("a") or finding.get("first") or ""),
                "b": str(finding.get("b") or finding.get("second") or ""),
            }
            if pair["a"] or pair["b"]:
                cluster_overlaps.append(pair)

    out_of_sheet = 0
    if usable:
        out_of_sheet = sum(1 for _, rect in parts if _rect_intersection(rect, usable) is None)
    summary: dict[str, Any] = {
        "partCount": len(parts),
        "partBboxCount": len(parts),
        "bodyOverlapCount": len(overlaps),
        "bodyOverlaps": overlaps,
        "clusterOverlapCount": len(cluster_overlaps),
        "clusterOverlaps": cluster_overlaps,
        "clusterEnvelopeOverlapCount": len(cluster_envelope_overlaps),
        "clusterEnvelopeOverlaps": cluster_envelope_overlaps,
        "outOfSheetCount": out_of_sheet,
        "sheetUsable": (
            {"minX": usable[0], "minY": usable[1], "maxX": usable[2], "maxY": usable[3]}
            if usable
            else None
        ),
        "sheetSource": usable_source or None,
        "titleBlock": (
            {
                "minX": title_block[0],
                "minY": title_block[1],
                "maxX": title_block[2],
                "maxY": title_block[3],
            }
            if title_block
            else None
        ),
    }
    if usable and _rect_area(usable) > 0:
        sheet_area = _rect_area(usable)
        title_area = _rect_area(_rect_intersection(usable, title_block)) if title_block else 0.0
        available_area = max(0.0, sheet_area - title_area)
        occupied = min(_union_area([rect for _, rect in parts]), available_area or sheet_area)
        occupancy = occupied / (available_area or sheet_area)
        ink_rects = [rect for _, rect in cluster_boxes]
        ink_occupied = min(_union_area(ink_rects), available_area or sheet_area) if ink_rects else None
        summary.update(
            {
                "occupiedArea": occupied,
                "occupiedRatio": occupancy,
                "availableArea": available_area or sheet_area,
                "titleBlockArea": title_area,
                "blankArea": max(0.0, (available_area or sheet_area) - occupied),
                "blankRatio": max(0.0, 1.0 - occupancy),
                "blankSpaceStatus": "high" if 1.0 - occupancy >= 0.8 else "measured",
                "inkOccupiedArea": ink_occupied,
                "inkOccupiedRatio": (ink_occupied / (available_area or sheet_area) if ink_occupied is not None else None),
                "inkBlankRatio": (1.0 - ink_occupied / (available_area or sheet_area) if ink_occupied is not None else None),
            }
        )
    else:
        summary.update(
            {
                "occupiedArea": None,
                "occupiedRatio": None,
                "blankArea": None,
                "blankRatio": None,
                "blankSpaceStatus": "unavailable",
                "availableArea": None,
                "titleBlockArea": None,
                "inkOccupiedArea": None,
                "inkOccupiedRatio": None,
                "inkBlankRatio": None,
            }
        )
    if not parts and usable:
        summary["blankSpaceStatus"] = "empty"
    return summary


# A stable, documented probe order.  Keep page probes separate so every page
# gets an authoritative active-page read instead of relying on --all-pages
# shallow data.
_PAGE_PROBES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("list", ("sch", "list", "--include-pins", "--include-bbox", "--page")),
    ("clusters", ("sch", "clusters", "--json", "--members", "--strict", "--doc")),
    ("layout-lint", ("sch", "layout-lint", "--json", "--strict", "--doc")),
    ("check", ("sch", "check", "--json", "--strict", "--page")),
    ("bridge-check", ("sch", "bridge-check", "--json", "--doc")),
    ("drc", ("sch", "drc", "--json", "--strict", "--doc")),
    ("gate", ("sch", "gate", "--json", "--strict", "--doc")),
    ("read", ("sch", "read", "--page")),
    # These diagnostics are independent of the gate and explain visual
    # whitespace/marker problems that the four gate stages cannot score.
    ("sheet-geometry", ("sch", "sheet-geometry", "--json", "--doc")),
    ("text-list", ("sch", "text-list", "--page")),
    ("layout-score", ("sch", "layout-score", "--json", "--doc")),
)


def _page_argv(prefix: tuple[str, ...], page_ref: str) -> list[str]:
    return [*prefix, page_ref]


def _call_runner(runner: Any, argv: list[str]) -> tuple[int | None, str, str]:
    target = runner.run if hasattr(runner, "run") else runner
    if not callable(target):
        raise TypeError("runner must be callable or expose run(argv)")
    result = target(argv)
    if not isinstance(result, tuple) or len(result) != 3:
        raise TypeError("runner must return (return_code, stdout, stderr)")
    rc, stdout, stderr = result
    try:
        normalised_rc = None if rc is None else int(rc)
    except (TypeError, ValueError):
        normalised_rc = None
    return normalised_rc, _normalise_output(stdout), _normalise_output(stderr)


def _effective_runner(
    runner: Any | None,
    adapter: Any | None,
    *,
    project: str | None,
    window: str | None,
) -> tuple[Any, dict[str, str]]:
    if runner is not None and adapter is not None:
        raise ValueError("pass either runner or adapter, not both")
    if runner is not None:
        return runner, {"project": project or "", "window": window or ""}
    if adapter is None:
        from edaloop.generate.adapter import EasyedaAdapter

        adapter = EasyedaAdapter(project=project)
        if window:
            # The adapter intentionally keeps window selection internal.  A
            # caller that explicitly supplies one has already made that
            # routing decision, so pin it without another discovery probe.
            adapter._window = window
            adapter._window_resolved = True
    return adapter, {"project": project or getattr(adapter, "_project", ""), "window": window or getattr(adapter, "_window", "")}


def _route_argv(argv: Sequence[str], scope: Mapping[str, str]) -> list[str]:
    """Append an explicit project/window route exactly once.

    ``EasyedaAdapter`` also pins routes, but doing this at the collector
    boundary makes injected runners deterministic and records the actual route
    in the manifest.  A window is preferred when both values are supplied,
    matching the adapter's routing precedence.
    """

    result = list(argv)
    # ``daemon health`` scans the daemon's listener and does not accept the
    # schematic routing flags.  Route selection is applied to the subsequent
    # page/document probes, while health remains the first unqualified probe.
    if result[:2] == ["daemon", "health"]:
        return result
    if "--window" not in result and "--project" not in result:
        window = str(scope.get("window") or "").strip()
        project = str(scope.get("project") or "").strip()
        if window:
            result.extend(["--window", window])
        elif project:
            result.extend(["--project", project])
    return result


def _artifact_paths(payload: Any) -> list[tuple[str, Mapping[str, Any]]]:
    found: list[tuple[str, Mapping[str, Any]]] = []
    root = _unwrap(payload)
    if not isinstance(root, Mapping):
        return found
    artifacts = root.get("artifacts")
    if isinstance(artifacts, list):
        for item in artifacts:
            if isinstance(item, Mapping):
                path = item.get("path") or item.get("artifactPath")
                if path:
                    found.append((str(path), item))
    path = root.get("artifactPath")
    if path:
        found.append((str(path), root))
    # De-duplicate by source path while retaining insertion order.
    result: list[tuple[str, Mapping[str, Any]]] = []
    seen: set[str] = set()
    for source, item in found:
        if source not in seen:
            seen.add(source)
            result.append((source, item))
    return result


def _capture_artifacts(
    payload: Any,
    out_dir: Path,
    command_index: int,
) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []
    target_dir = out_dir / "artifacts"
    for ordinal, (source_text, metadata) in enumerate(_artifact_paths(payload), 1):
        source = Path(source_text)
        record: dict[str, Any] = {
            "sourcePath": source_text,
            "metadata": dict(metadata),
        }
        if not source.exists() or not source.is_file():
            record["status"] = "missing"
            captured.append(record)
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_name = source.name.replace("..", "_") or f"artifact-{command_index}-{ordinal}"
        target = target_dir / f"{command_index:03d}-{ordinal:02d}-{safe_name}"
        try:
            shutil.copyfile(source, target)
            data = target.read_bytes()
            record.update(
                {
                    "status": "captured",
                    "path": str(target.relative_to(out_dir)).replace("\\", "/"),
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        except OSError as error:
            record.update({"status": "error", "error": f"{type(error).__name__}: {error}"})
        captured.append(record)
    return captured


def _write_command_files(
    out_dir: Path,
    index: int,
    stdout: str,
    stderr: str,
    raw_json: str | None,
) -> dict[str, str]:
    command_dir = out_dir / "commands"
    command_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{index:03d}"
    paths: dict[str, str] = {}
    for suffix, content, key in (
        ("stdout.txt", stdout, "stdoutPath"),
        ("stderr.txt", stderr, "stderrPath"),
    ):
        path = command_dir / f"{stem}-{suffix}"
        path.write_text(content, encoding="utf-8", newline="\n")
        paths[key] = str(path.relative_to(out_dir)).replace("\\", "/")
    if raw_json is not None:
        path = command_dir / f"{stem}-raw.json"
        path.write_text(raw_json + "\n", encoding="utf-8", newline="\n")
        paths["rawJsonPath"] = str(path.relative_to(out_dir)).replace("\\", "/")
    return paths


def collect_l0(
    out_dir: str | Path | None = None,
    *,
    runner: Any | None = None,
    adapter: Any | None = None,
    project: str | None = None,
    window: str | None = None,
    stop_on_health_failure: bool = False,
) -> dict[str, Any]:
    """Collect a read-only L0 snapshot and return its completed manifest.

    The health probe is always first.  By default subsequent probes still run
    after a health failure so the artifact records the complete failure shape;
    callers may opt into fail-fast behaviour with ``stop_on_health_failure``.
    Every generated argv is checked by :func:`is_read_only_argv` immediately
    before execution.
    """

    if out_dir is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out_dir = Path("runs") / "evidence" / f"l0-{stamp}-{uuid.uuid4().hex[:8]}"
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    effective_runner, scope = _effective_runner(runner, adapter, project=project, window=window)
    started = _utc_now()
    manifest: dict[str, Any] = {
        "schemaVersion": "edaloop.easyeda-evidence.l0.v1",
        "runId": uuid.uuid4().hex,
        "collector": "edaloop.evidence.collect_l0",
        "startedAt": started,
        "endedAt": None,
        "complete": False,
        "status": "running",
        "readOnly": True,
        "scope": scope,
        "healthOk": False,
        "pages": [],
        "plannedProbes": [],
        "commands": [],
        "pageReports": [],
        "artifacts": [],
        "errors": [],
    }
    partial_path = out_path / "manifest.incomplete.json"
    final_path = out_path / "manifest.json"
    _atomic_write_json(partial_path, manifest)

    page_probe_records: dict[str, dict[str, dict[str, Any]]] = {}

    def execute(name: str, argv: list[str], *, page: str | None = None) -> dict[str, Any]:
        argv = _route_argv(argv, scope)
        if not is_read_only_argv(argv):
            raise ReadOnlyViolation(f"probe '{name}' is not read-only: {argv!r}")
        manifest["plannedProbes"].append({"name": name, "page": page, "argv": list(argv)})
        _atomic_write_json(partial_path, manifest)
        index = len(manifest["commands"]) + 1
        started_at = _utc_now()
        monotonic_start = time.perf_counter()
        rc: int | None = None
        stdout = ""
        stderr = ""
        exception: str | None = None
        try:
            rc, stdout, stderr = _call_runner(effective_runner, argv)
        except Exception as error:  # evidence collection must preserve prior probes
            exception = f"{type(error).__name__}: {error}"
            manifest["errors"].append({"probe": name, "page": page, "error": exception})
        ended_at = _utc_now()
        payload, raw_json, parse_error = parse_json_output(stdout)
        paths = _write_command_files(out_path, index, stdout, stderr, raw_json)
        record: dict[str, Any] = {
            "index": index,
            "name": name,
            "page": page,
            "argv": list(argv),
            "startedAt": started_at,
            "endedAt": ended_at,
            "durationMs": round((time.perf_counter() - monotonic_start) * 1000, 3),
            "returnCode": rc,
            "stdout": stdout,
            "stderr": stderr,
            "stdoutSha256": _sha256_text(stdout),
            "stderrSha256": _sha256_text(stderr),
            "rawJsonSha256": _sha256_text(raw_json) if raw_json is not None else None,
            "parsedJson": payload,
            "rawJson": payload,
            "rawJsonText": raw_json,
            "jsonParseError": parse_error,
            "exception": exception,
            **paths,
        }
        if payload is not None:
            artifacts = _capture_artifacts(payload, out_path, index)
            if artifacts:
                record["artifacts"] = artifacts
                manifest["artifacts"].extend(artifacts)
        manifest["commands"].append(record)
        _atomic_write_json(partial_path, manifest)
        return record

    # 1) Health is deliberately the first actual EasyEDA call.
    health = execute("health", ["daemon", "health"])
    health_payload = health.get("parsedJson")
    health_status = (
        health_payload.get("status")
        if isinstance(health_payload, Mapping)
        else None
    )
    if health_status is None and isinstance(health_payload, Mapping):
        nested_health = health_payload.get("result")
        if isinstance(nested_health, Mapping):
            health_status = nested_health.get("status")
    manifest["healthOk"] = bool(
        health.get("returnCode") == 0
        and isinstance(health_payload, Mapping)
        and (
            str(health_status or "").casefold() in {"found", "ok"}
            or health_payload.get("ok") is True
            or isinstance(health_payload.get("found"), Mapping)
        )
    )
    if not manifest["healthOk"]:
        manifest["errors"].append({"probe": "health", "error": "health probe did not prove a connected daemon"})

    # 2) Page discovery.  It is still useful after a failed health call when a
    # fake runner or a recovering connector can answer it.
    pages_record = execute("pages", ["sch", "pages"])
    pages = _extract_pages(pages_record.get("parsedJson"))
    manifest["pages"] = pages
    if pages_record.get("returnCode") != 0 or pages_record.get("parsedJson") is None:
        manifest["errors"].append({"probe": "pages", "error": "page list unavailable or malformed"})

    if stop_on_health_failure and not manifest["healthOk"]:
        manifest["errors"].append({"probe": "collector", "error": "stopped after health failure"})
    else:
        for page in pages:
            ref = str(page["ref"])
            page_probe_records[ref] = {}
            for probe_name, prefix in _PAGE_PROBES:
                record = execute(probe_name, _page_argv(prefix, ref), page=ref)
                page_probe_records[ref][probe_name] = record

    # 3) Document-level views (run after page probes so the page evidence is
    # independently useful even if netlist export hangs/fails).
    nets = execute("nets", ["sch", "nets", "--json", "--strict"])
    netlist = execute("netlist", ["sch", "netlist"])

    page_reports: list[dict[str, Any]] = []
    for page in pages:
        ref = str(page["ref"])
        records = page_probe_records.get(ref, {})
        list_payload = (records.get("list") or {}).get("parsedJson")
        cluster_payload = (records.get("clusters") or {}).get("parsedJson")
        sheet_geometry_payload = (records.get("sheet-geometry") or {}).get("parsedJson")
        layout_payload = (records.get("layout-lint") or {}).get("parsedJson")
        layout = derive_layout_summary(
            list_payload,
            {"list": cluster_payload, "layout": layout_payload},
            sheet_geometry_payload,
        )
        score_payload = (records.get("layout-score") or {}).get("parsedJson")
        score_root = _unwrap(score_payload)
        text_payload = (records.get("text-list") or {}).get("parsedJson")
        text_root = _unwrap(text_payload)
        diagnostics = {
            "textCount": (
                text_root.get("count")
                if isinstance(text_root, Mapping) and isinstance(text_root.get("count"), (int, float))
                else len(text_root.get("texts", []))
                if isinstance(text_root, Mapping) and isinstance(text_root.get("texts"), list)
                else None
            ),
            "layoutScoreOverall": (
                score_root.get("overall")
                if isinstance(score_root, Mapping)
                else None
            ),
            "layoutScoreVerdict": (
                score_root.get("verdict")
                if isinstance(score_root, Mapping)
                else None
            ),
            "layoutScoreSkippedDims": (
                score_root.get("skippedDims")
                if isinstance(score_root, Mapping)
                else None
            ),
        }
        page_reports.append(
            {
                "page": page,
                "probeIndexes": {name: rec.get("index") for name, rec in records.items()},
                "returnCodes": {name: rec.get("returnCode") for name, rec in records.items()},
                "layoutSummary": layout,
                "diagnostics": diagnostics,
            }
        )
    manifest["pageReports"] = page_reports
    # A final/remainder page is allowed to be sparse.  This mirrors the layout
    # audit contract and prevents a mandatory A4 single-page schematic from
    # being labelled a packing failure solely because the circuit is small.
    blank_pages = [
        report["page"]["ref"]
        for report in page_reports[:-1]
        if report["layoutSummary"].get("blankSpaceStatus") in {"high", "empty"}
    ]
    overlap_pages = [
        report["page"]["ref"]
        for report in page_reports
        if report["layoutSummary"].get("bodyOverlapCount", 0)
        or report["layoutSummary"].get("clusterOverlapCount", 0)
    ]
    ratios = [
        report["layoutSummary"]["blankRatio"]
        for report in page_reports
        if isinstance(report["layoutSummary"].get("blankRatio"), (int, float))
    ]
    manifest["summaries"] = {
        "pageCount": len(pages),
        "probeCount": len(manifest["commands"]),
        "overlapPages": overlap_pages,
        "blankSpacePages": blank_pages,
        "averageBlankRatio": (sum(ratios) / len(ratios) if ratios else None),
        "netlistReturnCode": netlist.get("returnCode"),
        "netsReturnCode": nets.get("returnCode"),
        "nonZeroProbeCount": sum(
            1 for record in manifest["commands"] if record.get("returnCode") not in (0, None)
        ),
        "transportExceptionCount": sum(
            1 for record in manifest["commands"] if record.get("exception")
        ),
    }
    manifest["endedAt"] = _utc_now()
    manifest["complete"] = True
    manifest["status"] = "complete" if not manifest["errors"] else "complete-with-errors"
    manifest["manifestPath"] = str(final_path)
    # The final file appears in one atomic replace only after every planned
    # command and derived summary has been recorded.
    _atomic_write_json(final_path, manifest)
    _atomic_write_json(partial_path, manifest)
    return manifest


# Friendly aliases for callers and for the CLI aliases.
collect_evidence = collect_l0
summarize_layout = derive_layout_summary


__all__ = [
    "READ_ONLY_COMMANDS",
    "ReadOnlyViolation",
    "collect_evidence",
    "collect_l0",
    "derive_layout_summary",
    "is_read_only_argv",
    "parse_json_output",
    "summarize_layout",
]
