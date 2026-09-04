from __future__ import annotations

import json

import pytest

from edaloop.generate.adapter import AdapterError, EasyedaAdapter
from edaloop.generate.models import Action


def _adapter(handler) -> EasyedaAdapter:
    return EasyedaAdapter(runner=handler)


def test_explicit_window_route_wins_over_project_and_environment(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setenv("EDALOOP_WINDOW", "env-window")

    def runner(args):
        calls.append(list(args))
        return 0, "{}", ""

    adapter = EasyedaAdapter(
        runner=runner, project="demo-project", window="explicit-window"
    )
    adapter.run(["sch", "pages"])

    assert calls == [["sch", "pages", "--window", "explicit-window"]]


def test_check_version_ok() -> None:
    a = _adapter(lambda args: (0, "easyeda-agent v1.2.10\n", ""))
    assert a.check_version() == "1.2.10"


def test_check_version_mismatch_raises() -> None:
    a = _adapter(lambda args: (0, "easyeda-agent v1.1.1\n", ""))  # 旧钉扎=升级前实装(漂移形态)
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


def test_run_json_with_rc_preserves_command_status_and_stderr() -> None:
    a = _adapter(
        lambda args: (2, json.dumps({"verdict": "pass"}), "gate warning")
    )

    rc, payload, err = a.run_json_with_rc(["sch", "gate", "--json"])

    assert rc == 2
    assert payload == {"verdict": "pass"}
    assert err == "gate warning"


def test_subprocess_timeout_kills_tree_and_raises_adapter_error(monkeypatch) -> None:
    """真机 wedge 契约(run-955eb4729cff 冻死 38min):600s 超时必须树杀并抛
    AdapterError——subprocess.run(timeout=) 只杀直子进程,管道写端被孙进程
    继承时 communicate 永远等不到 EOF,run 无审计挂死。"""
    import subprocess as sp

    a = EasyedaAdapter()  # 走真 _subprocess_run,Popen 全部打桩
    calls: dict = {}

    class _FakeProc:
        pid = 4242

        def communicate(self, timeout=None):
            if calls.get("killed"):
                return "", ""
            raise sp.TimeoutExpired(cmd="easyeda x", timeout=timeout)

    def fake_popen(cmd, **kw):
        calls["popen"] = cmd
        return _FakeProc()

    def fake_taskkill(cmd, **kw):
        calls["killed"] = True
        calls["taskkill"] = cmd
        return sp.CompletedProcess(cmd, 0)

    monkeypatch.setattr(sp, "Popen", fake_popen)
    monkeypatch.setattr(sp, "run", fake_taskkill)
    monkeypatch.delenv("EASYEDA_BIN", raising=False)

    with pytest.raises(AdapterError, match="超时"):
        a._subprocess_run(["sch", "list"])
    assert calls["taskkill"][:3] == ["taskkill", "/PID", "4242"]
    assert "/T" in calls["taskkill"] and "/F" in calls["taskkill"]
