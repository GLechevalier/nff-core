from datetime import datetime

from pydantic import BaseModel, Field


class TimelineEntry(BaseModel):
    """One row of the crash-timeline — the single source of truth for who crashed
    and when.

    Built by joining a CrashRecord with its DiagnosisOutput (crash_class, location,
    confidence) and the ELFRecord behind its build_id (firmware_version, project,
    customer). Persisted append-only as JSONL.
    """

    crash_id: str
    device_id: str | None = None
    build_id: str
    firmware_version: str | None = None
    project: str | None = None
    customer: str | None = None
    received_at: datetime
    panic_type: str
    crash_class: str | None = None  # None until the crash has been diagnosed
    location: dict = Field(default_factory=dict)
    confidence: float | None = None
