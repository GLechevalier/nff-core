"""Local ESP32 crash classification — no network, no account, no API key.

Takes raw serial output containing an ESP-IDF panic (Guru Meditation, task/interrupt
watchdog, abort()/assert, or brownout), extracts the hard signals (EXCCAUSE, EXCVADDR,
PC, registers, backtrace), and classifies the crash against a small fault taxonomy
using deterministic rules only. It deliberately produces NO narrative: the output is
structured facts for a human or an LLM (e.g. the Claude Code session driving the MCP
server) to reason over.

Rules and formats are derived from the public ESP-IDF panic-handler output and the
Xtensa ISA exception causes (EXCCAUSE). Addresses stay unsymbolized here — resolving
them to function/file/line needs the build ELF, which is the platform `repair` path.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal, Optional

ENGINE = "nff-local-diagnose/0.1.0"

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

FaultFamily = Literal[
    "memory", "control_flow", "arithmetic", "assertion", "timing", "power", "unknown"
]


@dataclass(frozen=True)
class FaultClass:
    key: CrashClass
    title: str
    family: FaultFamily
    is_symptom: bool
    description: str
    remediation_hint: str


FAULT_TAXONOMY: dict[str, FaultClass] = {
    "null_deref": FaultClass(
        key="null_deref",
        title="Null-pointer dereference",
        family="memory",
        is_symptom=False,
        description="The firmware read or wrote through a NULL (or near-NULL) pointer.",
        remediation_hint="Trace where that pointer should have been assigned and fix the "
        "missing initialisation or lifetime bug; adding a null-check at the crash line "
        "only hides it.",
    ),
    "stack_overflow": FaultClass(
        key="stack_overflow",
        title="Stack overflow",
        family="control_flow",
        is_symptom=False,
        description="A task ran out of stack (deep/unbounded recursion or a large local "
        "allocation), smashing its own frames — the unwinder then repeats the same PCs.",
        remediation_hint="Remove the unbounded recursion or oversized stack allocation, or "
        "size the task stack for its real worst case; silencing the canary fixes nothing.",
    ),
    "watchdog": FaultClass(
        key="watchdog",
        title="Watchdog timeout",
        family="timing",
        is_symptom=True,
        description="A task held the CPU longer than the watchdog window. The reset is only "
        "the symptom — something blocked (busy-wait, deadlock, slow blocking I/O).",
        remediation_hint="Identify the code path that starved the CPU and unblock it. "
        "Feeding the watchdog more often, or disabling it, masks the symptom.",
    ),
    "heap_corruption": FaultClass(
        key="heap_corruption",
        title="Heap corruption",
        family="memory",
        is_symptom=False,
        description="An access through a stale or overrun heap pointer (use-after-free, "
        "double-free, buffer overrun) or a corrupted allocator link.",
        remediation_hint="Hunt the ownership/lifetime bug that scribbled on the heap — the "
        "faulting line is usually a victim, not the culprit.",
    ),
    "divide_by_zero": FaultClass(
        key="divide_by_zero",
        title="Integer divide by zero",
        family="arithmetic",
        is_symptom=False,
        description="An integer division or modulo hit a zero divisor (Xtensa EXCCAUSE 6).",
        remediation_hint="Work out why the divisor reached zero and fix it at the source; "
        "blindly clamping at the division site leaves the real bug in place.",
    ),
    "unaligned_access": FaultClass(
        key="unaligned_access",
        title="Unaligned memory access",
        family="memory",
        is_symptom=False,
        description="A load/store broke the required alignment (Xtensa EXCCAUSE 9) — "
        "commonly a cast to a wider type or a pointer into a packed struct.",
        remediation_hint="Correct the pointer's alignment (memcpy through a buffer or use "
        "a properly aligned type) rather than retrying the access.",
    ),
    "illegal_instruction": FaultClass(
        key="illegal_instruction",
        title="Illegal instruction",
        family="control_flow",
        is_symptom=False,
        description="The CPU decoded an invalid opcode (EXCCAUSE 0) with no stuck-unwinder "
        "pattern — usually control flow jumped through a corrupted pointer into data.",
        remediation_hint="Find what corrupted the function pointer or return target; the "
        "bad jump is downstream of an overwrite.",
    ),
    "bad_instruction_fetch": FaultClass(
        key="bad_instruction_fetch",
        title="Instruction fetch prohibited",
        family="control_flow",
        is_symptom=False,
        description="Execution landed in non-executable memory (EXCCAUSE 20) — a return "
        "address or function pointer was overwritten and pointed into data or NULL.",
        remediation_hint="Locate the overwrite (often a stack smash or use-after-free) that "
        "clobbered the return address; fixing the jump target treats the wrong thing.",
    ),
    "assert_failed": FaultClass(
        key="assert_failed",
        title="Assertion / abort",
        family="assertion",
        is_symptom=False,
        description="The firmware trapped on purpose via assert()/abort(); the failed "
        "expression states the invariant that broke.",
        remediation_hint="Fix whatever violated the asserted invariant; deleting or "
        "loosening the assert masks the symptom.",
    ),
    "brownout": FaultClass(
        key="brownout",
        title="Brownout reset",
        family="power",
        is_symptom=False,
        description="Supply voltage sagged below the brownout threshold — undersized "
        "supply, a current spike (e.g. radio TX), or poor regulation/decoupling.",
        remediation_hint="Fix the power delivery (supply headroom, decoupling, peak-current "
        "budget). This is a hardware/power problem, not a firmware bug.",
    ),
    "unknown": FaultClass(
        key="unknown",
        title="Unknown",
        family="unknown",
        is_symptom=False,
        description="No hard signal matched a known fault class.",
        remediation_hint="Collect more signal (a full backtrace, or the build ELF for "
        "symbolization via `repair`) before attempting a fix.",
    ),
}


def taxonomy_for(crash_class: str) -> FaultClass:
    """FaultClass metadata for a crash class, falling back to 'unknown'."""
    return FAULT_TAXONOMY.get(crash_class, FAULT_TAXONOMY["unknown"])


# ---------------------------------------------------------------------------
# Parsing — raw serial text → CrashFacts
# ---------------------------------------------------------------------------


@dataclass
class CrashFacts:
    panic_type: str = "unknown"  # brownout | idf_watchdog | abort | guru_meditation | unknown
    exception_cause: Optional[int] = None
    abort_reason: Optional[str] = None
    fault_pc: Optional[str] = None
    fault_addr: Optional[str] = None
    registers: dict[str, str] = field(default_factory=dict)
    backtrace: list[str] = field(default_factory=list)
    task_name: Optional[str] = None
    cpu: Optional[int] = None


# Format detection. Order matters: a task-watchdog dump ends by calling abort()
# ("abort() was called"), so the watchdog signature must be checked before the abort
# one — while a bare abort()/assert with no watchdog text is its own root cause.
# Brownout output is unambiguous and checked first.
_RE_BROWNOUT = re.compile(r"Brownout detector was triggered", re.IGNORECASE)
_RE_WATCHDOG = re.compile(
    r"Task watchdog got triggered|Interrupt wdt timeout|Guru Meditation Error.*?TWDT",
    re.IGNORECASE,
)
_RE_ABORT = re.compile(r"abort\(\) was called|assert failed:", re.IGNORECASE)
_RE_GURU = re.compile(
    r"Guru Meditation Error.*?Core\s+(\d+)\s+panic'ed", re.IGNORECASE | re.DOTALL
)

# Field extraction (ESP-IDF panic-handler output shapes).
_RE_ASSERT_MSG = re.compile(r"assert failed:\s*(.+)")
_RE_ABORT_AT = re.compile(
    r"abort\(\) was called at PC\s*(0x[0-9a-fA-F]+)(?:\s+on core\s*(\d+))?", re.IGNORECASE
)
_RE_EXCCAUSE = re.compile(r"EXCCAUSE\s*[:=]\s*(0x[0-9a-fA-F]+|\d+)")
_RE_EXCVADDR = re.compile(r"EXCVADDR\s*[:=]\s*(0x[0-9a-fA-F]+)")
_RE_PC = re.compile(r"\bPC\s*[:=]\s*(0x[0-9a-fA-F]+)")
_RE_CORE = re.compile(r"Core\s+(\d+)\s+panic'ed")
_RE_REGISTER = re.compile(r"\b(A\d{1,2}|PC|PS|SAR|EXCCAUSE|EXCVADDR)\s*[:=]\s*(0x[0-9a-fA-F]+)")
# "Backtrace: 0xPC:0xSP 0xPC:0xSP ..." — keep only the PC of each frame.
_RE_BACKTRACE = re.compile(
    r"Backtrace:\s*((?:0x[0-9a-fA-F]+(?::0x[0-9a-fA-F]+)?\s*)+)", re.IGNORECASE
)
_RE_HEX = re.compile(r"(0x[0-9a-fA-F]+)")
_RE_TASK = re.compile(
    r"Running task\s*['\"]?([^\s'\"]+)['\"]?|Task\s*['\"]([^'\"]+)['\"].*?watchdog",
    re.IGNORECASE,
)


def _frames(raw: str) -> list[str]:
    m = _RE_BACKTRACE.search(raw)
    if not m:
        return []
    frames = []
    for token in m.group(1).split():
        pcs = _RE_HEX.findall(token)
        if pcs:
            frames.append(pcs[0])
    return frames


def _task(raw: str) -> Optional[str]:
    m = _RE_TASK.search(raw)
    return (m.group(1) or m.group(2)) if m else None


def parse_crash(raw: str) -> CrashFacts:
    """Extract crash signals from raw serial text (format detected automatically)."""
    if _RE_BROWNOUT.search(raw):
        return CrashFacts(panic_type="brownout")

    if _RE_WATCHDOG.search(raw):
        return CrashFacts(panic_type="idf_watchdog", task_name=_task(raw))

    if _RE_ABORT.search(raw):
        facts = CrashFacts(panic_type="abort", backtrace=_frames(raw))
        m = _RE_ASSERT_MSG.search(raw)
        if m:
            # The assert line names the violated invariant — the strongest signal here.
            facts.abort_reason = f"assert failed: {m.group(1).strip()}"
        m = _RE_ABORT_AT.search(raw)
        if m:
            facts.fault_pc = m.group(1)
            if facts.abort_reason is None:
                facts.abort_reason = m.group(0).strip()
            if m.group(2) is not None:
                facts.cpu = int(m.group(2))
        return facts

    if _RE_GURU.search(raw):
        facts = CrashFacts(panic_type="guru_meditation", backtrace=_frames(raw))
        m = _RE_CORE.search(raw)
        if m:
            facts.cpu = int(m.group(1))
        m = _RE_EXCCAUSE.search(raw)
        if m:
            facts.exception_cause = int(m.group(1), 0)
        m = _RE_EXCVADDR.search(raw)
        if m:
            facts.fault_addr = m.group(1)
        m = _RE_PC.search(raw)
        if m:
            facts.fault_pc = m.group(1)
        facts.registers = {name: value for name, value in _RE_REGISTER.findall(raw)}
        facts.task_name = _task(raw)
        return facts

    return CrashFacts(panic_type="unknown")


# ---------------------------------------------------------------------------
# Classification — CrashFacts → ClassificationResult (deterministic rules)
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    crash_class: str
    explanation: str


@dataclass
class ClassificationResult:
    crash_class: str
    confidence: float
    rationale: str
    candidates: list[Candidate] = field(default_factory=list)


# Xtensa EXCCAUSE values (Xtensa ISA exception causes).
_XT_ILLEGAL_INSTRUCTION = 0
_XT_DIVIDE_BY_ZERO = 6
_XT_LOAD_STORE_ALIGNMENT = 9
_XT_INSTR_FETCH_PROHIBITED = 20
_XT_LOAD_PROHIBITED = 28
_XT_STORE_PROHIBITED = 29

# Near-NULL window: an access below this is a NULL pointer plus a small struct offset.
_NULL_WINDOW = 0xFF
# ESP32 DRAM heap region (approximate) — used only to flag heap-interior fault addresses.
_HEAP_LOW = 0x3FFB0000
_HEAP_HIGH = 0x3FFF0000


def _addr(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _near_null(value: Optional[str]) -> bool:
    v = _addr(value)
    return v is not None and v <= _NULL_WINDOW


def _in_heap(value: Optional[str]) -> bool:
    v = _addr(value)
    return v is not None and _HEAP_LOW <= v <= _HEAP_HIGH


def _stuck_unwinder(backtrace: list[str], min_repeat: int = 3) -> bool:
    return any(n >= min_repeat for n in Counter(backtrace).values())


def classify(facts: CrashFacts) -> ClassificationResult:
    """Rule-based classification from hard signals only. Pure — no I/O, no LLM."""
    cause = facts.exception_cause
    addr = facts.fault_addr

    # Panic format alone is decisive for these three.
    if facts.panic_type == "idf_watchdog":
        return ClassificationResult(
            "watchdog", 0.97,
            "IDF watchdog format detected (no register dump) — a task starved the CPU.",
        )
    if facts.panic_type == "abort":
        reason = facts.abort_reason or "abort() was called"
        return ClassificationResult(
            "assert_failed", 0.95, f"Deliberate trap via abort()/assert(): {reason}",
        )
    if facts.panic_type == "brownout":
        return ClassificationResult(
            "brownout", 0.96,
            "Brownout detector fired — supply voltage dropped below threshold.",
        )

    # Prohibited store/load at a near-NULL address is a textbook null dereference.
    if cause == _XT_STORE_PROHIBITED and _near_null(addr):
        return ClassificationResult(
            "null_deref", 0.96,
            f"StoreProhibited (EXCCAUSE=29) writing near-NULL address {addr}.",
        )
    if cause == _XT_STORE_PROHIBITED:
        return ClassificationResult(
            "null_deref", 0.75,
            f"StoreProhibited (EXCCAUSE=29) at {addr}; not near NULL — could be a "
            "misaligned or corrupted pointer write.",
            [Candidate("heap_corruption",
                       "A store fault away from NULL can also be a corrupted heap pointer.")],
        )
    if cause == _XT_LOAD_PROHIBITED and _near_null(addr):
        return ClassificationResult(
            "null_deref", 0.96,
            f"LoadProhibited (EXCCAUSE=28) reading near-NULL address {addr}.",
        )

    # Unambiguous EXCCAUSE-keyed faults win over the shape-based rules below.
    if cause == _XT_DIVIDE_BY_ZERO:
        return ClassificationResult(
            "divide_by_zero", 0.97,
            "IntegerDivideByZero (EXCCAUSE=6): integer division/modulo by zero.",
        )
    if cause == _XT_LOAD_STORE_ALIGNMENT:
        return ClassificationResult(
            "unaligned_access", 0.95,
            f"LoadStoreAlignment (EXCCAUSE=9) at {addr}: access broke required alignment.",
        )
    if cause == _XT_INSTR_FETCH_PROHIBITED:
        return ClassificationResult(
            "bad_instruction_fetch", 0.90,
            f"InstrFetchProhibited (EXCCAUSE=20) at PC {facts.fault_pc}: executing "
            "non-executable memory — a clobbered return address or function pointer.",
            [Candidate("stack_overflow",
                       "A smashed stack overwrites return addresses, landing fetches in data.")],
        )

    # A backtrace looping on the same PC means the unwinder is stuck on a corrupt
    # stack. Checked before address-range rules: overflow into DRAM produces a
    # heap-range fault_addr that would otherwise look like heap corruption.
    if _stuck_unwinder(facts.backtrace):
        return ClassificationResult(
            "stack_overflow", 0.93,
            "Backtrace repeats the same PC 3+ times — unwinder stuck on a corrupt stack frame.",
            [Candidate("heap_corruption",
                       "A corrupted heap link can also loop the unwinder.")]
            if _in_heap(addr) else [],
        )
    if cause == _XT_ILLEGAL_INSTRUCTION and _stuck_unwinder(facts.backtrace, min_repeat=2):
        return ClassificationResult(
            "stack_overflow", 0.85,
            "IllegalInstruction with repeated backtrace PCs — SP likely corrupted, "
            "CPU executing stack data.",
        )
    if cause == _XT_ILLEGAL_INSTRUCTION:
        return ClassificationResult(
            "illegal_instruction", 0.80,
            "IllegalInstruction (EXCCAUSE=0): invalid opcode — likely a corrupted code "
            "pointer or a jump into data.",
            [Candidate("bad_instruction_fetch",
                       "A corrupted call target can also fault as a prohibited fetch.")],
        )

    if cause == _XT_LOAD_PROHIBITED and _in_heap(addr):
        return ClassificationResult(
            "heap_corruption", 0.72,
            f"LoadProhibited (EXCCAUSE=28) at heap-interior address {addr}.",
            [Candidate("null_deref",
                       "A heap-range address can also be NULL plus a large struct offset.")],
        )
    if cause == _XT_LOAD_PROHIBITED:
        return ClassificationResult(
            "null_deref", 0.60,
            f"LoadProhibited (EXCCAUSE=28) at {addr}; address in neither the NULL window "
            "nor the heap range.",
            [Candidate("heap_corruption",
                       "An unclassifiable load fault may be a dangling or corrupted pointer.")],
        )

    return ClassificationResult(
        "unknown", 0.30,
        f"No hard signal matched (panic_type={facts.panic_type!r}, EXCCAUSE={cause}, "
        f"fault_addr={addr}).",
        [Candidate("null_deref",
                   "A null-pointer access cannot be ruled out without a clear EXCCAUSE.")],
    )


# ---------------------------------------------------------------------------
# Entry point — raw serial text → structured-facts dict
# ---------------------------------------------------------------------------

_EXCERPT_LINES = 60


def _excerpt(raw: str) -> str:
    return "\n".join(raw.splitlines()[:_EXCERPT_LINES])


def diagnose(raw: str) -> dict:
    """Parse + classify a crash. Returns structured facts only — never a narrative.

    When no panic signature is found the result is an honest {"ok": False, ...};
    a classification is never fabricated from unrecognized text.
    """
    facts = parse_crash(raw or "")
    if facts.panic_type == "unknown":
        return {
            "ok": False,
            "engine": ENGINE,
            "error": "no ESP32 panic signature found in input",
            "raw_excerpt": _excerpt(raw or ""),
        }

    result = classify(facts)
    meta = taxonomy_for(result.crash_class)
    return {
        "ok": True,
        "engine": ENGINE,
        "crash_class": result.crash_class,
        "title": meta.title,
        "family": meta.family,
        "is_symptom": meta.is_symptom,
        "confidence": result.confidence,
        "rationale": result.rationale,
        "candidates": [
            {"crash_class": c.crash_class, "explanation": c.explanation}
            for c in result.candidates
        ],
        "description": meta.description,
        "remediation_hint": meta.remediation_hint,
        "panic_type": facts.panic_type,
        "exception_cause": facts.exception_cause,
        "fault_pc": facts.fault_pc,
        "fault_addr": facts.fault_addr,
        "registers": facts.registers,
        "backtrace": facts.backtrace,
        "task_name": facts.task_name,
        "cpu": facts.cpu,
        "abort_reason": facts.abort_reason,
        "raw_excerpt": _excerpt(raw),
        "note": "addresses are unsymbolized; `repair` (platform login) resolves them "
                "to function/file/line using the uploaded build ELF",
    }
