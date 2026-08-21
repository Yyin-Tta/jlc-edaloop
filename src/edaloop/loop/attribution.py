from __future__ import annotations

from edaloop.validate.models import Finding

_FIX_INSTRUCTION = {
    "REPLAN": "重新规划:按 evidence 调整块选择/端口绑定",
    "REBIND_NET": "把缺失的网络补进对应块的绑定:upstream 块用 ports_binding,place 通道器件用 pins_binding(引脚号→网名);同名引脚族(VSS*/VDD*/EP)都要绑",
    "RELAYOUT": "放置几何问题:给冲突块设置 params.spacing(如 300;过宽块编译器会自动截到 A4 内)或 at 空位(A4 页内坐标,x∈[100,1100] y∈[300,780];编译器优先读这两个字段)",
    "REWIRE": "连线问题:检查悬空/短路网络的重叠与绑定",
    "ADD_BLOCK": "知识库无对应块:确认 uncovered 列表是否已如实登记,不要发明器件",
    "RETRY_ENV": "环境问题(连接器/窗口/页面):无需改 plan,重试即可",
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
        instr = _FIX_INSTRUCTION.get(f.suggested_fix_class, _FIX_INSTRUCTION["REPLAN"])
        where = f.where.pin and f"{f.where.ref}:{f.where.pin}" or (f.where.net or f.where.ref or "-")
        lines.append(f"[{f.code}@{where}] {f.evidence} → 修复:{instr}")
    if weak:
        lines.append(f"[IR_UNCOVERED x{len(weak)}] 弱门禁(不阻断):保持 uncovered 如实登记即可")
    return "\n".join(lines)
