from __future__ import annotations

from edaloop.validate.models import Finding

_FIX_INSTRUCTION = {
    "REPLAN": "重新规划:按 evidence 调整块选择/端口绑定",
    "REBIND_NET": "把缺失的网络补进对应块的绑定:upstream 块用 ports_binding,place 通道器件用 pins_binding(引脚号→网名);同名引脚族(VSS*/VDD*/EP)都要绑",
    "RELAYOUT": "放置几何问题:给冲突块设置 params.spacing(如 300;过宽块编译器会自动截到 A4 内)或 at 空位(A4 页内坐标,x∈[100,1100] y∈[300,780];编译器优先读这两个字段)",
    "REWIRE": "连线问题:检查悬空/短路网络的重叠与绑定",
    "ADD_BLOCK": "知识库无对应块:确认 uncovered 列表是否已如实登记,不要发明器件",
    "RETRY_ENV": "环境问题(连接器/窗口/页面):无需改 plan,重试即可",
    # Terminal layout checks distinguish a readback failure from a real
    # circuit/layout defect.  Keep that distinction in planner feedback so a
    # stale connector response does not trigger needless re-planning.
    "RETRY_READBACK": "回读证据不足:重读当前页并确认返回非空、结构完整的器件/引脚数据;不要改电路计划",
    "DEDUPE_MARKER": "标记整理:删除同一 pin/net 的重复 netport/netflag/netlabel,保留一个后重新回读",
    "RESEAT_MARKER": "标记可读性:优先执行 sch destagger --doc <页> --apply;不要直接移动 marker 坐标以免脱离桩线",
    "REPACK": "分页/利用率:合并同模块小块并重新运行确定性 repack;不要只收紧间距",
}

_CODE_FIX_CLASS = {
    "GATE_UNVERIFIED": "RETRY_ENV",
    "LAYOUT_READ_UNVERIFIED": "RETRY_READBACK",
    "LAYOUT_SNAPSHOT_INVALID": "RETRY_READBACK",
    "LAYOUT_MARKER_ON_BODY": "RESEAT_MARKER",
    "LAYOUT_PAGE_INK_SPARSE": "REPACK",
}


def attribute(findings: list[Finding]) -> str:
    """findings → 定向反馈文本(喂给下一轮 planner)。"""
    if not findings:
        return ""
    blocking = [f for f in findings if not f.weak]
    weak = [f for f in findings if f.weak]
    lines: list[str] = []
    seen: set[str] = set()
    for f in blocking:
        key = (f.code, f.where.ref, f.where.net, f.where.pin)
        if key in seen:
            continue
        seen.add(key)
        fix_class = f.suggested_fix_class or _CODE_FIX_CLASS.get(f.code, "REPLAN")
        instr = _FIX_INSTRUCTION.get(fix_class, _FIX_INSTRUCTION["REPLAN"])
        where = f.where.pin and f"{f.where.ref}:{f.where.pin}" or (f.where.net or f.where.ref or "-")
        lines.append(f"[{f.code}@{where}] {f.evidence} → 修复:{instr}")
    if weak:
        from collections import Counter

        tag = "、".join(f"{code}x{n}" for code, n in Counter(f.code for f in weak).items())
        lines.append(f"[弱门禁 x{len(weak)}] {tag}(不阻断):uncovered 保持如实登记;数据债回填块库 electrical 字段后自动转强判")
        # P4-4③:PARAM_OFF_SPEC 逐条展开(选值 vs 建议值是可执行的修正指令,压成计数丢信息)
        for f in [x for x in weak if x.code == "PARAM_OFF_SPEC"][:5]:
            where = f.where.pin and f"{f.where.ref}:{f.where.pin}" or (f.where.net or f.where.ref or "-")
            lines.append(f"[{f.code}@{where}] {f.evidence} → 修正:按建议值换 resistor-std/capacitor-std(params.value 取表内值)")
    return "\n".join(lines)
