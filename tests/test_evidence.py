from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from edaloop.evidence import (
    ReadOnlyViolation,
    collect_l0,
    derive_layout_summary,
    is_read_only_argv,
    parse_json_output,
)


def _fixture_runner(calls: list[list[str]]):
    def run(argv: list[str]):
        calls.append(list(argv))
        if argv[:2] == ["daemon", "health"]:
            return 0, "version note\n" + json.dumps({"status": "found"}), ""
        if argv[:2] == ["sch", "pages"]:
            return 0, json.dumps({"result": {"pages": [{"name": "P1", "uuid": "page-1"}, {"name": "P2", "uuid": "page-2"}]}}), ""
        if argv[1] == "list":
            page = argv[-1]
            return 0, json.dumps(
                {
                    "result": {
                        "components": [
                            {"componentType": "sheet", "bbox": {"minX": 0, "minY": 0, "maxX": 100, "maxY": 100}},
                            {"componentType": "part", "designator": f"R{page[-1]}A", "bbox": {"minX": 10, "minY": 10, "maxX": 40, "maxY": 40}},
                            {"componentType": "part", "designator": f"R{page[-1]}B", "bbox": {"minX": 30, "minY": 30, "maxX": 50, "maxY": 50}},
                        ]
                    }
                }
            ), ""
        if argv[1] == "clusters":
            return 0, json.dumps({"clusters": [], "findings": []}), ""
        if argv[1] == "netlist":
            return 0, "artifact saved\n" + json.dumps({"result": {"artifactPath": "missing.enet"}}), ""
        return 0, json.dumps({"ok": True}), ""

    return run


def test_probe_matrix_is_read_only_and_health_is_first(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    manifest = collect_l0(tmp_path, runner=_fixture_runner(calls))

    assert manifest["complete"] is True
    assert manifest["healthOk"] is True
    assert calls[0][:2] == ["daemon", "health"]
    assert len(manifest["commands"]) == len(manifest["plannedProbes"]) == 2 + 2 * 11 + 2
    assert all(is_read_only_argv(call) for call in calls)
    mutators = {"clear", "place", "modify", "save", "wire", "connect", "disconnect", "block-apply", "page-new", "page-delete"}
    assert not any(len(call) > 1 and call[1] in mutators for call in calls if call[0] == "sch")
    assert (tmp_path / "manifest.json").exists()
    assert (tmp_path / "manifest.incomplete.json").exists()

    first = manifest["commands"][0]
    assert first["stdoutSha256"] == hashlib.sha256(first["stdout"].encode()).hexdigest()
    assert first["rawJson"] == {"status": "found"}
    assert Path(tmp_path / first["stdoutPath"]).read_text(encoding="utf-8") == first["stdout"]
    assert manifest["pageReports"][0]["layoutSummary"]["bodyOverlapCount"] == 1
    assert manifest["pageReports"][0]["layoutSummary"]["blankSpaceStatus"] == "high"
    assert manifest["summaries"]["blankSpacePages"] == ["P1"]


def test_nonzero_and_malformed_json_are_retained_and_collection_completes(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str]):
        calls.append(list(argv))
        if argv[:2] == ["daemon", "health"]:
            return 1, "not-json", "daemon down"
        if argv[:2] == ["sch", "pages"]:
            return 0, json.dumps({"pages": [{"name": "P1"}]}), ""
        return 7, "broken output", "probe failed"

    manifest = collect_l0(tmp_path, runner=run)
    assert manifest["complete"] is True
    assert manifest["status"] == "complete-with-errors"
    assert manifest["commands"][0]["returnCode"] == 1
    assert manifest["commands"][0]["stderr"] == "daemon down"
    assert manifest["commands"][0]["jsonParseError"]
    assert manifest["commands"][2]["returnCode"] == 7
    assert manifest["commands"][2]["stdout"] == "broken output"
    assert manifest["commands"][2]["rawJson"] is None
    assert len(calls) == len(manifest["commands"])


def test_layout_summary_uses_union_area_and_cluster_findings() -> None:
    list_payload = {
        "components": [
            {"componentType": "sheet", "bbox": {"minX": 0, "minY": 0, "maxX": 10, "maxY": 10}},
            {"componentType": "part", "designator": "A", "bbox": {"minX": 0, "minY": 0, "maxX": 4, "maxY": 4}},
            {"componentType": "part", "designator": "B", "bbox": {"minX": 2, "minY": 2, "maxX": 6, "maxY": 6}},
        ]
    }
    clusters_payload = {"clusters": [], "findings": [{"type": "overlap", "a": "A", "b": "B"}]}
    summary = derive_layout_summary(list_payload, clusters_payload)
    assert summary["bodyOverlapCount"] == 1
    assert summary["clusterOverlapCount"] == 1
    # 16 + 16 - 4 = 28 occupied units over a 100-unit sheet.
    assert summary["occupiedArea"] == pytest.approx(28)
    assert summary["blankRatio"] == pytest.approx(0.72)


def test_cluster_envelope_intersection_is_diagnostic_without_checker_finding() -> None:
    list_payload = {
        "components": [
            {"componentType": "sheet", "bbox": {"minX": 0, "minY": 0, "maxX": 100, "maxY": 100}},
            {"componentType": "part", "designator": "U1", "bbox": {"minX": 20, "minY": 20, "maxX": 30, "maxY": 30}},
            {"componentType": "part", "designator": "R1", "bbox": {"minX": 40, "minY": 20, "maxX": 45, "maxY": 25}},
        ]
    }
    clusters_payload = {
        "clusters": [
            {"designator": "U1", "box": {"minX": 10, "minY": 10, "maxX": 42, "maxY": 40}},
            {"designator": "R1", "box": {"minX": 40, "minY": 15, "maxX": 60, "maxY": 35}},
        ],
        "findings": [],
    }

    summary = derive_layout_summary(list_payload, clusters_payload)

    assert summary["bodyOverlapCount"] == 0
    assert summary["clusterOverlapCount"] == 0
    assert summary["clusterEnvelopeOverlapCount"] == 1


def test_json_parser_handles_diagnostic_prefix() -> None:
    payload, raw, error = parse_json_output("warning\n{" + '"ok":true}' )
    assert payload == {"ok": True}
    assert raw == '{"ok":true}'
    assert error is None


def test_read_only_guard_rejects_mutating_shapes() -> None:
    assert is_read_only_argv(["sch", "read", "--page", "P1"])
    assert is_read_only_argv(["sch", "gate", "--json", "--strict"])
    assert not is_read_only_argv(["sch", "clear", "--doc", "P1"])
    assert not is_read_only_argv(["sch", "layout-lint", "--apply"])
    assert not is_read_only_argv(["sch", "place"])


def test_project_and_window_routes_are_explicit_and_window_wins(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    manifest = collect_l0(
        tmp_path,
        runner=_fixture_runner(calls),
        project="demo",
        window="window-1",
    )
    assert manifest["scope"] == {"project": "demo", "window": "window-1"}
    # daemon health scans the listener and does not accept schematic routing
    # flags; every subsequent probe must remain pinned to the requested window.
    assert calls[0][:2] == ["daemon", "health"]
    assert all("--window" in call and "window-1" in call for call in calls[1:])
    assert all("--project" not in call for call in calls)


def test_sheet_text_and_score_diagnostics_are_recorded(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str]):
        calls.append(list(argv))
        if argv[:2] == ["daemon", "health"]:
            return 0, json.dumps({"status": "found"}), ""
        if argv[:2] == ["sch", "pages"]:
            return 0, json.dumps({"pages": [{"name": "P1"}]}), ""
        if argv[1] == "list":
            return 0, json.dumps({"components": []}), ""
        if argv[1] == "clusters":
            return 0, json.dumps({"clusters": []}), ""
        if argv[1] == "sheet-geometry":
            return 0, json.dumps({"result": {"sheet": {"bbox": {"minX": 0, "minY": 0, "maxX": 100, "maxY": 100}}, "titleBlock": {"bbox": {"minX": 80, "minY": 0, "maxX": 100, "maxY": 20}}}}), ""
        if argv[1] == "text-list":
            return 0, json.dumps({"result": {"count": 3, "texts": [{}, {}, {}]}}), ""
        if argv[1] == "layout-score":
            return 0, json.dumps({"overall": 72, "verdict": "fair", "skippedDims": 1}), ""
        return 0, json.dumps({"ok": True}), ""

    manifest = collect_l0(tmp_path, runner=run)
    report = manifest["pageReports"][0]
    assert report["diagnostics"] == {
        "textCount": 3,
        "layoutScoreOverall": 72,
        "layoutScoreVerdict": "fair",
        "layoutScoreSkippedDims": 1,
    }
    assert report["layoutSummary"]["titleBlockArea"] == 400
