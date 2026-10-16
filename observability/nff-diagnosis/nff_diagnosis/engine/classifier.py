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
_EXCCAUSE_DIVIDE_BY_ZERO = 6           # IntegerDivideByZero
_EXCCAUSE_UNALIGNED = 9                # LoadStoreAlignmentCause
_EXCCAUSE_INSTR_FETCH_PROHIBITED = 20  # 0x14 — fetch from non-executable page

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

        # Rule 1b: abort()/assert is a deliberate trap — its own root cause, not a watchdog.
        if crash.panic_type == "abort":
            reason = crash.abort_reason or "abort() was called"
            return ClassificationResult(
                crash_class="assert_failed",
                confidence=0.95,
                rationale=f"Firmware called abort()/assert(): {reason}",
            )

        # Rule 1c: brownout — a power fault, no code signature.
        if crash.panic_type == "brownout":
            return ClassificationResult(
                crash_class="brownout",
                confidence=0.96,
                rationale="Brownout detector triggered; supply voltage dipped below threshold.",
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

        # Rule 4b: definitive EXCCAUSE-keyed faults (arithmetic / alignment / bad fetch).
        # These causes are unambiguous, so they win over the shape-based rules below.
        if cause == _EXCCAUSE_DIVIDE_BY_ZERO:
            return ClassificationResult(
                crash_class="divide_by_zero",
                confidence=0.97,
                rationale="IntegerDivideByZero (EXCCAUSE=6): integer division or modulo by a zero divisor.",
            )
        if cause == _EXCCAUSE_UNALIGNED:
            return ClassificationResult(
                crash_class="unaligned_access",
                confidence=0.95,
                rationale=f"LoadStoreAlignment (EXCCAUSE=9) at {addr}: access violated required alignment.",
            )
        if cause == _EXCCAUSE_INSTR_FETCH_PROHIBITED:
            return ClassificationResult(
                crash_class="bad_instruction_fetch",
                confidence=0.90,
                rationale=(
                    f"InstrFetchProhibited (EXCCAUSE=20) at PC {crash.fault_pc}: executing "
                    "non-executable memory — a corrupted return address or function pointer."
                ),
                candidates=[
                    Candidate(
                        crash_class="stack_overflow",
                        explanation="A smashed stack can overwrite the return address, landing the fetch in data.",
                    )
                ],
            )

        # Rule 5: repeated PCs → stack_overflow (evaluated before address-range rules because
        # a stack overflow into DRAM will produce a heap-range fault_addr, making it look like
        # heap_corruption; the unwinder-stuck pattern is a stronger signal than the address alone).
        if _has_repeated_pcs(crash.backtrace):
            return ClassificationResult(
                crash_class="stack_overflow",
                confidence=0.93,
                rationale="Backtrace contains 3+ identical PCs; unwinder is stuck on a corrupt stack frame.",
                candidates=[
                    Candidate(
                        crash_class="heap_corruption",
                        explanation="A corrupted heap link can also trap the unwinder in a loop.",
                    )
                ] if _is_heap_range(addr) else [],
            )

        # Rule 6: IllegalInstruction + repeated PCs (SP corruption)
        if cause == _EXCCAUSE_ILLEGAL_INSTRUCTION and _has_repeated_pcs(crash.backtrace, min_repeat=2):
            return ClassificationResult(
                crash_class="stack_overflow",
                confidence=0.85,
                rationale="IllegalInstruction with repeated backtrace PCs; SP likely corrupted, CPU executing stack data.",
            )

        # Rule 6b: IllegalInstruction without a stuck-unwinder signature → illegal_instruction.
        if cause == _EXCCAUSE_ILLEGAL_INSTRUCTION:
            return ClassificationResult(
                crash_class="illegal_instruction",
                confidence=0.80,
                rationale=(
                    "IllegalInstruction (EXCCAUSE=0): the CPU decoded an invalid opcode — "
                    "likely a corrupted code pointer or a jump into data."
                ),
                candidates=[
                    Candidate(
                        crash_class="bad_instruction_fetch",
                        explanation="A corrupted call target can also trap as a prohibited instruction fetch.",
                    )
                ],
            )

        # Rule 7: LoadProhibited + heap interior address → heap_corruption (ambiguous)
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
