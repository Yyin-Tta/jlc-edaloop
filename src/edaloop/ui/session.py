"""UI 会话层纯逻辑:目录约定 + audit 事件 → 用户可读行。

不 import chainlit(可独立测试、可被任意前端复用);app.py 只是它的展示层。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

UI_ROOT = Path("runs/ui")


def _safe(name: str) -> str:
    """路径名消毒:非字母数字-_与点之外一律 _,去前导点防穿越;保留扩展名。"""
    cleaned = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:80]
    return cleaned.lstrip(".") or "unnamed"


def session_dir(session_id: str) -> Path:
    """会话目录 runs/ui/<id>/(attachments/ 一并建好);runs/ 已 gitignore。"""
    d = UI_ROOT / _safe(session_id) / "attachments"
    d.mkdir(parents=True, exist_ok=True)
    return d.parent


def save_attachment(session_id: str, name: str, content: bytes) -> Path:
    p = session_dir(session_id) / "attachments" / _safe(name)
    p.write_bytes(content)
    return p


def format_event(kind: str, fields: dict[str, Any]) -> str | None:
    """audit 事件 → 聊天流一行;None = 观察类/噪音事件,不打扰用户。

    只翻译用户需要跟着走的节点:轮计划/校验、落图成败、gate 非 pass、
    收口/HALT/交付。lib-search、pin-verify、sizing 等细节留在审计目录。
    """
    r = fields.get("round_no")
    if kind == "ir":
        return f"— IR 解析:{fields.get('source')}(rev {fields.get('revision')})"
    if kind == "round-plan":
        unc = len(fields.get("uncovered") or [])
        return f"— 轮 {r} plan:{len(fields.get('blocks') or [])} blocks,uncovered {unc}"
    if kind == "round-validate":
        return (
            f"— 轮 {r} 校验:gate={fields.get('gate')},"
            f"blocking={len(fields.get('blocking') or [])},weak={len(fields.get('weak') or [])}"
        )
    if kind in ("block-apply", "sch-place"):
        inst = fields.get("instance") or fields.get("designator") or "?"
        status = fields.get("status") or ("applied" if fields.get("ok") else "failed")
        mark = "✓" if status == "applied" else f"✗ {status}"
        return f"— {mark} {inst}@{fields.get('page') or 'P1'}"
    if kind == "apply-fatal":
        err = str(fields.get("error") or "")[:80]
        return f"— ✗ apply-fatal {fields.get('instance') or '?'}:{err}"
    if kind == "gate":
        verdict = fields.get("verdict")
        return None if verdict == "pass" else f"— gate {fields.get('page')}:{verdict}"
    if kind == "page-clear":
        fails = fields.get("failures") or []
        tail = f"(失败 {len(fails)})" if fails else ""
        return f"— 清页 {','.join(fields.get('pages') or [])}{tail}"
    if kind == "arrange-result":
        return f"— 布局收口 {fields.get('page')}:残留 {fields.get('remaining')}"
    if kind == "loop-done":
        return f"— 结束:{fields.get('status')}(共 {fields.get('rounds')} 轮)"
    if kind == "loop-halt":
        return f"— HALT:{fields.get('reason')}"
    if kind == "refine-retry":
        return f"— 二次检索:{len(fields.get('queries') or [])} 条"
    if kind == "freeform-augment":
        added = fields.get("added") or []
        return f"— 模式增强 {fields.get('pattern')}:+{len(added)} blocks"
    if kind == "case-writeback" and fields.get("inserted"):
        return f"— 案例回写:{fields.get('case_id')}"
    if kind == "delivery":
        arts = fields.get("artifacts") or {}
        return f"— 交付:{', '.join(arts)}" if arts else None
    return None
