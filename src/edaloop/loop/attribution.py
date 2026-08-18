from __future__ import annotations

from edaloop.validate.models import Finding

_FIX_INSTRUCTION = {
    "REPLAN": "重新规划:按 evidence 调整块选择/端口绑定",
    "REBIND_NET": "把缺失的网络补进对应块的端口绑定(如 LDO 输出口绑到该轨名)",
    "RELAYOUT": "放置几何问题:给冲突块设置 params.spacing=500,或换 --at 空位",
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
        k = (f.code, f.suggested_fix_class)
        if k in seen:
            continue
        seen.add(k)
        instr = _FIX_INSTRUCTION.get(f.suggested_fix_class, _FIX_INSTRUCTION["REPLAN"])
        where = f.where.net or f.where.ref or "-"
        lines.append(f"[{f.code}@{where}] {f.evidence} → 修复:{instr}")
    if weak:
        lines.append(f"[IR_UNCOVERED x{len(weak)}] 弱门禁(不阻断):保持 uncovered 如实登记即可")
    return "\n".join(lines)
