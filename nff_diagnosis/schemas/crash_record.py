import uuid
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field


class CrashRecord(BaseModel):
    crash_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    build_id: str
    device_id: Optional[str] = None
    panic_type: str  # "guru_meditation" | "idf_watchdog" | "unknown"
    exception_cause: Optional[int] = None
    fault_pc: Optional[str] = None
    fault_addr: Optional[str] = None
    registers: dict[str, str] = Field(default_factory=dict)
    backtrace: list[str] = Field(default_factory=list)
    task_name: Optional[str] = None
    cpu: Optional[int] = None
    core_dump: Optional[str] = None
    raw: str
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
