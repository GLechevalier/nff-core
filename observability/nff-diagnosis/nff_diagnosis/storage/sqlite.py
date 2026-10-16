import sqlite3
from pathlib import Path

from nff_diagnosis.schemas import CrashRecord
from nff_diagnosis.schemas.diagnosis_output import DiagnosisOutput
from nff_diagnosis.storage.base import StorageBase

_DDL = """
CREATE TABLE IF NOT EXISTS crashes (
    crash_id TEXT PRIMARY KEY,
    json     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS diagnoses (
    crash_id TEXT PRIMARY KEY,
    json     TEXT NOT NULL
);
"""


class SQLiteStorage(StorageBase):
    def __init__(self, db_path: Path) -> None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_DDL)
        self._conn.commit()

    def save_crash(self, record: CrashRecord) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO crashes(crash_id, json) VALUES (?, ?)",
            (record.crash_id, record.model_dump_json()),
        )
        self._conn.commit()

    def save_diagnosis(self, diag: DiagnosisOutput) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO diagnoses(crash_id, json) VALUES (?, ?)",
            (diag.crash_id, diag.model_dump_json()),
        )
        self._conn.commit()

    def get_crash(self, crash_id: str) -> CrashRecord | None:
        row = self._conn.execute(
            "SELECT json FROM crashes WHERE crash_id = ?", (crash_id,)
        ).fetchone()
        return CrashRecord.model_validate_json(row[0]) if row else None

    def get_diagnosis(self, crash_id: str) -> DiagnosisOutput | None:
        row = self._conn.execute(
            "SELECT json FROM diagnoses WHERE crash_id = ?", (crash_id,)
        ).fetchone()
        return DiagnosisOutput.model_validate_json(row[0]) if row else None

    def list_crashes(self, limit: int = 50, offset: int = 0) -> list[CrashRecord]:
        rows = self._conn.execute(
            "SELECT json FROM crashes ORDER BY rowid DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [CrashRecord.model_validate_json(r[0]) for r in rows]
