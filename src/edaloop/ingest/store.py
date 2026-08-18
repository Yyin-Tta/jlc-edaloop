from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from edaloop.ingest.models import IngestReport, PinTable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS datasheets (
    rowid INTEGER PRIMARY KEY,
    part TEXT UNIQUE NOT NULL,
    pdf TEXT NOT NULL,
    pin_count INTEGER,
    verdict TEXT,
    report TEXT,
    pins TEXT,
    ingested_at TEXT DEFAULT (datetime('now'))
)
"""


class DatasheetStore:
    def __init__(self, path: str = "runs/knowledge.db") -> None:
        self.conn = sqlite3.connect(path)
        self.conn.execute(_SCHEMA)
        self.conn.commit()

    def upsert(self, table: PinTable, report: IngestReport) -> None:
        self.conn.execute(
            "INSERT INTO datasheets(part, pdf, pin_count, verdict, report, pins) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(part) DO UPDATE SET pdf=excluded.pdf, pin_count=excluded.pin_count, "
            "verdict=excluded.verdict, report=excluded.report, pins=excluded.pins, ingested_at=datetime('now')",
            (
                table.part,
                table.source_pdf,
                report.pin_count,
                report.verdict,
                report.model_dump_json(),
                table.model_dump_json(),
            ),
        )
        self.conn.commit()

    def get(self, part: str) -> PinTable | None:
        row = self.conn.execute(
            "SELECT pins FROM datasheets WHERE part = ?", (part,)
        ).fetchone()
        if not row:
            return None
        return PinTable.model_validate_json(row[0])

    def close(self) -> None:
        self.conn.close()
