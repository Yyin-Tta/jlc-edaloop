from __future__ import annotations

from pathlib import Path

from edaloop.generate.adapter import EasyedaAdapter
from edaloop.generate.audit import AuditLog
from edaloop.intent.ir import DesignIR
from edaloop.llm.fake import FakeChat
from edaloop.loop.controller import LoopController


def _ir() -> DesignIR:
    return DesignIR.model_validate(
        {
            "source": "strict-policy.md",
            "power": {"rails": [{"name": "VCC", "voltage": 3.3}]},
        }
    )


def _controller(tmp_path: Path, adapter) -> LoopController:
    return LoopController(
        _ir(), {}, lambda _query: [], FakeChat("[]"), adapter,
        AuditLog(tmp_path / "audit"),
    )


def test_environment_cannot_disable_strict_layout_for_real_adapter(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("EDALOOP_STRICT_LAYOUT", "0")

    adapter = EasyedaAdapter(runner=lambda _args: (0, "", ""))
    controller = _controller(tmp_path, adapter)

    assert controller.strict_layout is True


def test_explicit_false_cannot_disable_strict_layout_for_real_adapter(
    tmp_path: Path,
) -> None:
    adapter = EasyedaAdapter(runner=lambda _args: (0, "", ""))
    controller = LoopController(
        _ir(), {}, lambda _query: [], FakeChat("[]"), adapter,
        AuditLog(tmp_path / "audit"), strict_layout=False,
    )

    assert controller.strict_layout is True


def test_environment_can_select_strict_mode_for_injected_adapter(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("EDALOOP_STRICT_LAYOUT", "1")

    class FakeAdapter:
        pass

    controller = _controller(tmp_path, FakeAdapter())

    assert controller.strict_layout is True
