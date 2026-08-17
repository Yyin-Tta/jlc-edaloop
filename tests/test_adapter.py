from __future__ import annotations

import json

import pytest

from edaloop.generate.adapter import AdapterError, EasyedaAdapter
from edaloop.generate.models import Action


def _adapter(handler) -> EasyedaAdapter:
    return EasyedaAdapter(runner=handler)


def test_check_version_ok() -> None:
    a = _adapter(lambda args: (0, "easyeda-agent v0.25.1\n", ""))
    assert a.check_version() == "0.25.1"


def test_check_version_mismatch_raises() -> None:
    a = _adapter(lambda args: (0, "easyeda-agent v0.26.0\n", ""))
    with pytest.raises(AdapterError, match="ADR-0002"):
        a.check_version()


def test_daemon_health_ok() -> None:
    a = _adapter(lambda args: (0, json.dumps({"status": "found"}), ""))
    assert a.daemon_health()["status"] == "found"


def test_daemon_health_not_found() -> None:
    a = _adapter(lambda args: (0, json.dumps({"status": "none"}), ""))
    with pytest.raises(AdapterError):
        a.daemon_health()


def test_apply_and_gate_flow() -> None:
    calls: list[list[str]] = []

    def handler(args: list[str]) -> tuple[int, str, str]:
        calls.append(args)
        if args[0] == "sch" and args[1] == "block-apply":
            return 0, json.dumps({"status": "success"}), ""
        if args[0] == "sch" and args[1] == "gate":
            return 1, json.dumps({"verdict": "pass", "stages": []}), ""
        return 1, "", "boom"

    a = _adapter(handler)
    actions = [
        Action(kind="block-apply", block_instance="ldo1", upstream_id="b", args=["sch", "block-apply", "b", "--json"], desc=""),
        Action(kind="sch-gate", args=["sch", "gate", "--json"], desc=""),
    ]
    results = a.apply_and_gate(actions)
    assert results[0]["status"] == "success"
    assert results[1]["verdict"] == "pass"


def test_run_json_parse_error_raises() -> None:
    a = _adapter(lambda args: (0, "not json", ""))
    with pytest.raises(AdapterError, match="JSON 解析失败"):
        a.run_json(["x"])
