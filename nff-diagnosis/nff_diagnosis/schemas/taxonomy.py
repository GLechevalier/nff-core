"""The fault taxonomy — metadata for every CrashClass, in one inspectable place.

The classifier (engine/classifier.py) decides *which* CrashClass a crash is; this module
says *what that class means*: which family of root cause it belongs to, whether it is a
symptom that usually masks a deeper cause, a one-line description, and a remediation hint.

Keeping this beside the CrashClass literal (schemas/diagnosis_output.py) makes the taxonomy
a real, extensible object graph rather than a bare string set: adding a root cause is one
entry here plus one classifier rule. `is_symptom` is what makes "a fix that masks the
symptom instead of the root cause" expressible at the schema level — see FAULT_TAXONOMY
below (watchdog is the canonical symptom layer).
"""

from __future__ import annotations

from dataclasses import dataclass

from nff_diagnosis.schemas.diagnosis_output import CrashClass, FaultFamily


@dataclass(frozen=True)
class FaultClass:
    key: CrashClass
    title: str
    family: FaultFamily
    is_symptom: bool
    description: str
    remediation_hint: str


FAULT_TAXONOMY: dict[CrashClass, FaultClass] = {
    "null_deref": FaultClass(
        key="null_deref",
        title="Null-pointer dereference",
        family="memory",
        is_symptom=False,
        description="A load or store through a NULL (or near-NULL) pointer.",
        remediation_hint="Find the pointer that is NULL at the fault site and guard it, or "
        "fix why it was never initialised — do not merely null-check at the crash line.",
    ),
    "stack_overflow": FaultClass(
        key="stack_overflow",
        title="Stack overflow",
        family="control_flow",
        is_symptom=False,
        description="The task stack was exhausted (unbounded recursion or a too-small stack), "
        "corrupting the frame so the unwinder loops on repeated PCs.",
        remediation_hint="Bound the recursion / large stack allocation, or raise the task "
        "stack size at its root — not by suppressing the canary.",
    ),
    "watchdog": FaultClass(
        key="watchdog",
        title="Watchdog timeout",
        family="timing",
        is_symptom=True,
        description="A task starved the CPU past the watchdog window. The timeout is the "
        "SYMPTOM; the root cause is whatever blocked (busy-loop, deadlock, long I/O).",
        remediation_hint="Find and fix the blocking/starving code path. Feeding or disabling "
        "the watchdog is a SYMPTOM MASK, not a fix.",
    ),
    "heap_corruption": FaultClass(
        key="heap_corruption",
        title="Heap corruption",
        family="memory",
        is_symptom=False,
        description="A corrupted heap link or an access through a dangling/overrun heap "
        "pointer (use-after-free, double-free, out-of-bounds write).",
        remediation_hint="Fix the ownership/lifetime bug that corrupts the block; the crash "
        "site is usually far from the root cause.",
    ),
    "divide_by_zero": FaultClass(
        key="divide_by_zero",
        title="Integer divide by zero",
        family="arithmetic",
        is_symptom=False,
        description="An integer division or modulo by a zero divisor (Xtensa EXCCAUSE=6).",
        remediation_hint="Guard or fix the divisor at its source; understand why it reached "
        "zero rather than clamping blindly at the division.",
    ),
    "unaligned_access": FaultClass(
        key="unaligned_access",
        title="Unaligned memory access",
        family="memory",
        is_symptom=False,
        description="A load/store that violated the required alignment (Xtensa EXCCAUSE=9), "
        "typically a mis-aligned cast or packed-struct pointer.",
        remediation_hint="Fix the pointer alignment / use a memcpy or aligned type; do not "
        "just retry the access.",
    ),
    "illegal_instruction": FaultClass(
        key="illegal_instruction",
        title="Illegal instruction",
        family="control_flow",
        is_symptom=False,
        description="The CPU decoded an invalid opcode (EXCCAUSE=0) without a stuck-unwinder "
        "signature — usually a corrupted code pointer or a jump into data.",
        remediation_hint="Trace the corrupted function pointer / return target; alignment or "
        "overwrite of a code pointer is the likely root cause.",
    ),
    "bad_instruction_fetch": FaultClass(
        key="bad_instruction_fetch",
        title="Instruction fetch prohibited",
        family="control_flow",
        is_symptom=False,
        description="The CPU tried to execute from non-executable memory (EXCCAUSE=20) — a "
        "corrupted return address or function pointer landed in data/NULL.",
        remediation_hint="Find what overwrote the return address / function pointer (often a "
        "stack smash or use-after-free); fix that, not the jump.",
    ),
    "assert_failed": FaultClass(
        key="assert_failed",
        title="Assertion / abort",
        family="assertion",
        is_symptom=False,
        description="Firmware called abort()/assert() — a deliberate trap. The failed "
        "expression names the violated invariant.",
        remediation_hint="Fix the condition that violated the asserted invariant; removing or "
        "weakening the assert is a SYMPTOM MASK.",
    ),
    "brownout": FaultClass(
        key="brownout",
        title="Brownout reset",
        family="power",
        is_symptom=False,
        description="The brownout detector fired: the supply voltage dipped below threshold "
        "(inadequate power, a current spike, or bad regulation).",
        remediation_hint="Address the power delivery (decoupling, supply headroom, peak "
        "current); this is a hardware/power root cause, not a code bug.",
    ),
    "unknown": FaultClass(
        key="unknown",
        title="Unknown",
        family="unknown",
        is_symptom=False,
        description="No hard signal matched a known fault class.",
        remediation_hint="Gather more signal (full backtrace, ELF for symbolication) before "
        "attempting a fix.",
    ),
}


def taxonomy_for(crash_class: CrashClass) -> FaultClass:
    """Look up the FaultClass metadata for a crash class (falls back to 'unknown')."""
    return FAULT_TAXONOMY.get(crash_class, FAULT_TAXONOMY["unknown"])
