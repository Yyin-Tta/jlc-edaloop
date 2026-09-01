from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# UI 事件总线挂点:audit 是全链路唯一事件汇(controller/pipeline 全部经此落盘),
# UI 只挂 listener 观察而不侵入业务。
EventListener = Callable[[str, dict[str, Any]], None]


class AuditLog:
    def __init__(self, run_dir: str | Path, *, listener: EventListener | None = None) -> None:
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "audit.jsonl"
        self._listener = listener

    def event(self, kind: str, **fields) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if self._listener is not None:
            try:  # 观察者异常绝不拖垮主链路(同 delivery 各增强通道纪律)
                self._listener(kind, fields)
            except Exception:
                pass

    def save_json(self, name: str, obj: object) -> Path:
        p = self.dir / name
        p.write_text(
            json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
        )
        return p
