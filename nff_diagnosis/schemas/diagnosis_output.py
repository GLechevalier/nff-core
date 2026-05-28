from datetime import datetime, timezone
from typing import Literal, Optional
from pydantic import BaseModel, Field

CrashClass = Literal["null_deref", "stack_overflow", "watchdog", "heap_corruption", "unknown"]


class ResolvedFrame(BaseModel):
    pc: str
    function: Optional[str] = None
    file: Optional[str] = None
    line: Optional[int] = None


class Candidate(BaseModel):
    crash_class: CrashClass
    explanation: str


class DiagnosisOutput(BaseModel):
    crash_id: str
    crash_class: CrashClass
    root_cause: str
    location: dict  # {function, file, line} of primary frame; may be empty dict
    resolved_backtrace: list[ResolvedFrame]
    confidence: float
    candidates: list[Candidate] = Field(default_factory=list)
    engine_version: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
