from __future__ import annotations

from edaloop.generate.models import Action, BlockPlan
from edaloop.knowledge.models import BlockRecord

_GND_HINTS = ("GND", "AGND", "DGND", "PGND", "E", "VSS")
_PWR_HINTS = ("VCC", "VDD", "COM", "VBAT", "VIN", "VSYS")


def _sanitize_designator(instance: str) -> str:
    d = "".join(c for c in instance.upper() if c.isalnum())
    return d[:8] if d else "U1"


class CompileError(Exception):
    pass


def _pin_kind(pin_name: str) -> str:
    n = pin_name.upper()
    if any(h in n for h in _GND_HINTS):
        return "gnd"
    if any(h in n for h in _PWR_HINTS):
        return "power"
    return "netport"


def _fill_bindings(plan: BlockPlan, catalog: dict[str, BlockRecord]) -> BlockPlan:
    for b in plan.blocks:
        rec = catalog.get(b.block_id)
        if rec is None:
            raise CompileError(f"块 {b.block_id} 不在库中")
        if rec.upstream is not None:
            if b.upstream_id != rec.upstream.id:
                raise CompileError(
                    f"块 {b.block_id} 的 upstream_id {b.upstream_id} 与库中 {rec.upstream.id} 不一致"
                )
            ports = rec.upstream.ports
            unknown = [p for p in b.ports_binding if p not in ports]
            if unknown:
                raise CompileError(f"块 {b.block_id} 绑定了不存在的端口: {unknown}")
            for port, default_net in ports.items():
                b.ports_binding.setdefault(port, default_net)
        else:
            if not rec.lcsc:
                raise CompileError(f"块 {b.block_id} 无 upstream 且无 lcsc(不可落图)")
            if not b.pins_binding:
                raise CompileError(
                    f"块 {b.block_id} 是库外器件(place 通道),必须给出 pins_binding(pin号→网络)"
                )
            if rec.pinout:
                unknown = [p for p in b.pins_binding if p not in rec.pinout]
                if unknown:
                    raise CompileError(f"块 {b.block_id} 绑定了不存在的引脚号: {unknown}")
    return plan


_GRID_X0 = 400
_GRID_Y0 = 300
_GRID_DX = 2200
_GRID_DY = 1800
_GRID_COLS = 4


def _grid_at(index: int) -> str:
    col = index % _GRID_COLS
    row = index // _GRID_COLS
    return f"{_GRID_X0 + col * _GRID_DX},{_GRID_Y0 + row * _GRID_DY}"


def compile_actions(
    plan: BlockPlan,
    catalog: dict[str, BlockRecord],
    *,
    spacing_default: str = "400",
) -> list[Action]:
    plan = _fill_bindings(plan, catalog)
    actions: list[Action] = []
    grid_idx = 0
    for b in plan.blocks:
        rec = catalog[b.block_id]
        if not b.at:
            b.at = _grid_at(grid_idx)
        grid_idx += 1
        if rec.upstream is not None:
            args = [
                "sch",
                "block-apply",
                b.upstream_id,
                "--instance",
                b.instance,
                "--spacing",
                spacing_default,
                "--at",
                b.at,
            ]
            for port, net in b.ports_binding.items():
                args += ["--bind", f"{port}={net}"]
            args.append("--json")
            actions.append(
                Action(
                    kind="block-apply",
                    block_instance=b.instance,
                    upstream_id=b.upstream_id,
                    args=args,
                    desc=f"{rec.name} @ {b.at} -> {b.ports_binding}",
                )
            )
        else:
            designator = _sanitize_designator(b.instance)
            x, y = b.at.split(",")
            b.params["x"], b.params["y"] = x, y
            place = [
                "sch",
                "place",
                "--lib",
                b.params.get("lib_uuid", ""),
                "--uuid",
                b.params.get("device_uuid", ""),
                "--x",
                b.params.get("x", "400"),
                "--y",
                b.params.get("y", "300"),
                "--designator",
                designator,
            ]
            actions.append(
                Action(
                    kind="lib-search",
                    block_instance=b.instance,
                    lcsc=rec.lcsc or "",
                    mpn=(rec.parts[0].ref if rec.parts else ""),
                    args=["lib", "search", "--query", rec.lcsc or "", "--limit", "3"],
                    desc=f"查 {rec.lcsc} 的库 uuid(place 前置,C 号无映射时回退 MPN)",
                )
            )
            actions.append(
                Action(
                    kind="sch-place",
                    block_instance=b.instance,
                    args=place,
                    pinout=dict(rec.pinout) if rec.pinout else None,
                    desc=f"{rec.name}({rec.lcsc}) 直放",
                )
            )
            pinout = rec.pinout or {}
            for pin, net in b.pins_binding.items():
                pin_name = pinout.get(pin, pin)
                kind = _pin_kind(pin_name)
                actions.append(
                    Action(
                        kind="sch-autoconnect",
                        block_instance=b.instance,
                        args=[
                            "sch",
                            "autoconnect",
                            "--pin",
                            f"{designator}:{pin_name}",
                            "--kind",
                            kind,
                            "--net",
                            net,
                        ],
                        desc=f"{designator}:{pin}({pin_name}) -> {net}",
                    )
                )
    actions.append(
        Action(kind="sch-gate", block_instance="", upstream_id="", args=["sch", "gate", "--json"], desc="验证门禁")
    )
    return actions
