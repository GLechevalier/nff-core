from datetime import datetime, timezone
from typing import Literal, Optional
from pydantic import BaseModel, Field

# The generalized fault taxonomy. Each member is a distinct *root cause* keyed on a HARD
# crash signal (EXCCAUSE / panic format / backtrace shape) — see engine/classifier.py.
# The metadata (family, is_symptom, remediation) lives in schemas/taxonomy.py, keyed by
# these strings. Keep this literal and FAULT_TAXONOMY in lockstep.
CrashClass = Literal[
    "null_deref",
    "stack_overflow",
    "watchdog",
    "heap_corruption",
    "divide_by_zero",
    "unaligned_access",
    "illegal_instruction",
    "bad_instruction_fetch",
    "assert_failed",
    "brownout",
    "unknown",
]

# Coarse grouping a crash class belongs to — lets consumers reason about the kind of
# root cause (and, with is_symptom, whether a fix is addressing cause vs symptom).
FaultFamily = Literal[
    "memory",
    "control_flow",
    "arithmetic",
    "assertion",
    "timing",
    "power",
    "unknown",
]


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
    # Taxonomy metadata, populated from FAULT_TAXONOMY by the engine. Defaulted here so
    # directly-constructed / older-persisted rows round-trip without them.
    family: FaultFamily = "unknown"
    is_symptom: bool = False  # True → this class masks a deeper root cause (e.g. watchdog)
    engine_version: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
