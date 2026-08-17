from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    def __init__(self, run_dir: str | Path) -> None:
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "audit.jsonl"

    def event(self, kind: str, **fields) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def save_json(self, name: str, obj: object) -> Path:
        p = self.dir / name
        p.write_text(
            json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
        )
        return p
