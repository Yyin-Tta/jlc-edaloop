from __future__ import annotations

from edaloop.generate.models import Action, BlockPlan
from edaloop.knowledge.models import BlockRecord


class CompileError(Exception):
    pass


def _fill_bindings(plan: BlockPlan, catalog: dict[str, BlockRecord]) -> BlockPlan:
    for b in plan.blocks:
        rec = catalog.get(b.block_id)
        if rec is None or rec.upstream is None:
            raise CompileError(f"块 {b.block_id} 不在库中或无 upstream 映射")
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
    return plan


def compile_actions(
    plan: BlockPlan,
    catalog: dict[str, BlockRecord],
    *,
    spacing_default: str = "400",
) -> list[Action]:
    plan = _fill_bindings(plan, catalog)
    actions: list[Action] = []
    for b in plan.blocks:
        rec = catalog[b.block_id]
        args = [
            "sch",
            "block-apply",
            b.upstream_id,
            "--instance",
            b.instance,
            "--spacing",
            b.params.get("spacing", spacing_default),
        ]
        if b.at:
            args += ["--at", b.at]
        for port, net in b.ports_binding.items():
            args += ["--bind", f"{port}={net}"]
        args.append("--json")
        actions.append(
            Action(
                kind="block-apply",
                block_instance=b.instance,
                upstream_id=b.upstream_id,
                args=args,
                desc=f"{rec.name} -> {b.ports_binding}",
            )
        )
    actions.append(
        Action(kind="sch-gate", block_instance="", upstream_id="", args=["sch", "gate", "--json"], desc="验证门禁")
    )
    return actions
