from collections import Counter
from dataclasses import dataclass, field

from nff_diagnosis.schemas import CrashRecord
from nff_diagnosis.schemas.diagnosis_output import Candidate, CrashClass

# Xtensa ESP32 EXCCAUSE values (Xtensa ISA Ref, Table 4-64)
_EXCCAUSE_STORE_PROHIBITED = 29  # 0x1d — store to non-writable/execute-only page
_EXCCAUSE_LOAD_PROHIBITED = 28   # 0x1c — load from non-readable page
_EXCCAUSE_ILLEGAL_INSTRUCTION = 0
_EXCCAUSE_DEBUG = 5
_EXCCAUSE_DEBUG_ALT = 1

# ESP32 heap DRAM range (approximate)
_HEAP_LOW = 0x3FFB0000
_HEAP_HIGH = 0x3FFF0000


@dataclass
class ClassificationResult:
    crash_class: CrashClass
    confidence: float
    rationale: str
    candidates: list[Candidate] = field(default_factory=list)


def _is_null_range(addr: str | None) -> bool:
    if addr is None:
        return False
    try:
        return int(addr, 16) <= 0xFF
    except ValueError:
        return False


def _is_heap_range(addr: str | None) -> bool:
    if addr is None:
        return False
    try:
        v = int(addr, 16)
        return _HEAP_LOW <= v <= _HEAP_HIGH
    except ValueError:
        return False


def _has_repeated_pcs(backtrace: list[str], min_repeat: int = 3) -> bool:
    return any(v >= min_repeat for v in Counter(backtrace).values())


class RuleClassifier:
    """Pure rule-based classifier — no I/O, no external calls."""

    def classify(self, crash: CrashRecord) -> ClassificationResult:
        cause = crash.exception_cause
        addr = crash.fault_addr

        # Rule 1: watchdog format is definitive
        if crash.panic_type == "idf_watchdog":
            return ClassificationResult(
                crash_class="watchdog",
                confidence=0.97,
                rationale="IDF watchdog task-list format detected; no register dump present.",
            )

        # Rule 2: StoreProhibited + null-range address → null_deref (high confidence)
        if cause == _EXCCAUSE_STORE_PROHIBITED and _is_null_range(addr):
            return ClassificationResult(
                crash_class="null_deref",
                confidence=0.96,
                rationale=f"StoreProhibited (EXCCAUSE=29) writing to null-range address {addr}.",
            )

        # Rule 3: StoreProhibited, address outside null range
        if cause == _EXCCAUSE_STORE_PROHIBITED:
            return ClassificationResult(
                crash_class="null_deref",
                confidence=0.75,
                rationale=f"StoreProhibited (EXCCAUSE=29) at {addr}; address outside null range — could be misaligned write.",
                candidates=[
                    Candidate(
                        crash_class="heap_corruption",
                        explanation="Non-null store-prohibited fault can also indicate a corrupted pointer in heap memory.",
                    )
                ],
            )

        # Rule 4: LoadProhibited + null-range address → null_deref (high confidence)
        if cause == _EXCCAUSE_LOAD_PROHIBITED and _is_null_range(addr):
            return ClassificationResult(
                crash_class="null_deref",
                confidence=0.96,
                rationale=f"LoadProhibited (EXCCAUSE=28) reading from null-range address {addr}.",
            )

        # Rule 5: LoadProhibited + heap interior address → heap_corruption (ambiguous)
        if cause == _EXCCAUSE_LOAD_PROHIBITED and _is_heap_range(addr):
            return ClassificationResult(
                crash_class="heap_corruption",
                confidence=0.72,
                rationale=f"LoadProhibited (EXCCAUSE=28) at heap interior address {addr}.",
                candidates=[
                    Candidate(
                        crash_class="null_deref",
                        explanation="Heap-range fault_addr could be an offset from a NULL pointer rather than a corrupted heap link.",
                    )
                ],
            )

        # Rule 6: repeated PCs in backtrace → stack overflow (unwinder stuck)
        if _has_repeated_pcs(crash.backtrace):
            return ClassificationResult(
                crash_class="stack_overflow",
                confidence=0.93,
                rationale="Backtrace contains 3+ identical PCs; unwinder is stuck on a corrupt stack frame.",
            )

        # Rule 7: IllegalInstruction + repeated PCs (SP corruption)
        if cause == _EXCCAUSE_ILLEGAL_INSTRUCTION and _has_repeated_pcs(crash.backtrace, min_repeat=2):
            return ClassificationResult(
                crash_class="stack_overflow",
                confidence=0.85,
                rationale="IllegalInstruction with repeated backtrace PCs; SP likely corrupted, CPU executing stack data.",
            )

        # Rule 8: LoadProhibited, address not in known ranges
        if cause == _EXCCAUSE_LOAD_PROHIBITED:
            return ClassificationResult(
                crash_class="null_deref",
                confidence=0.60,
                rationale=f"LoadProhibited (EXCCAUSE=28) at {addr}; address not in heap or null range.",
                candidates=[
                    Candidate(
                        crash_class="heap_corruption",
                        explanation="Unknown load-prohibited address may indicate a dangling or corrupted pointer.",
                    )
                ],
            )

        # Fallthrough
        rationale = (
            f"No hard signal matched (panic_type={crash.panic_type!r}, "
            f"EXCCAUSE={cause}, fault_addr={addr})."
        )
        return ClassificationResult(
            crash_class="unknown",
            confidence=0.30,
            rationale=rationale,
            candidates=[
                Candidate(
                    crash_class="null_deref",
                    explanation="Cannot rule out a null-pointer access without a clear EXCCAUSE.",
                )
            ],
        )
