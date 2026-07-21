//! Local ESP32 crash classification — no network, no account, no API key.
//!
//! Takes raw serial output containing an ESP-IDF panic (Guru Meditation, task/interrupt
//! watchdog, abort()/assert, or brownout), extracts the hard signals (EXCCAUSE, EXCVADDR,
//! PC, registers, backtrace), and classifies the crash against a small fault taxonomy
//! using deterministic rules only. It deliberately produces NO narrative: the output is
//! structured facts for a human or an LLM (e.g. the Claude Code session driving the MCP
//! server) to reason over.
//!
//! Rules and formats are derived from the public ESP-IDF panic-handler output and the
//! Xtensa ISA exception causes (EXCCAUSE). Addresses stay unsymbolized here — resolving
//! them to function/file/line needs the build ELF, which is the platform `repair` path.
//!
//! Faithful port of the Python `nff/tools/diagnose.py`; the string fixtures in the test
//! module are shared verbatim with `tests/test_diagnose.py` as the behavioral parity oracle.

use std::collections::HashMap;
use std::sync::OnceLock;

use regex::Regex;
use serde_json::{json, Map, Value};

pub const ENGINE: &str = "nff-local-diagnose/0.1.0";

// ---------------------------------------------------------------------------
// Fault taxonomy
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy)]
pub struct FaultClass {
    pub key: &'static str,
    pub title: &'static str,
    pub family: &'static str,
    pub is_symptom: bool,
    pub description: &'static str,
    pub remediation_hint: &'static str,
}

pub const FAULT_TAXONOMY: &[FaultClass] = &[
    FaultClass {
        key: "null_deref",
        title: "Null-pointer dereference",
        family: "memory",
        is_symptom: false,
        description: "The firmware read or wrote through a NULL (or near-NULL) pointer.",
        remediation_hint: "Trace where that pointer should have been assigned and fix the \
            missing initialisation or lifetime bug; adding a null-check at the crash line \
            only hides it.",
    },
    FaultClass {
        key: "stack_overflow",
        title: "Stack overflow",
        family: "control_flow",
        is_symptom: false,
        description: "A task ran out of stack (deep/unbounded recursion or a large local \
            allocation), smashing its own frames — the unwinder then repeats the same PCs.",
        remediation_hint: "Remove the unbounded recursion or oversized stack allocation, or \
            size the task stack for its real worst case; silencing the canary fixes nothing.",
    },
    FaultClass {
        key: "watchdog",
        title: "Watchdog timeout",
        family: "timing",
        is_symptom: true,
        description: "A task held the CPU longer than the watchdog window. The reset is only \
            the symptom — something blocked (busy-wait, deadlock, slow blocking I/O).",
        remediation_hint: "Identify the code path that starved the CPU and unblock it. \
            Feeding the watchdog more often, or disabling it, masks the symptom.",
    },
    FaultClass {
        key: "heap_corruption",
        title: "Heap corruption",
        family: "memory",
        is_symptom: false,
        description: "An access through a stale or overrun heap pointer (use-after-free, \
            double-free, buffer overrun) or a corrupted allocator link.",
        remediation_hint: "Hunt the ownership/lifetime bug that scribbled on the heap — the \
            faulting line is usually a victim, not the culprit.",
    },
    FaultClass {
        key: "divide_by_zero",
        title: "Integer divide by zero",
        family: "arithmetic",
        is_symptom: false,
        description: "An integer division or modulo hit a zero divisor (Xtensa EXCCAUSE 6).",
        remediation_hint: "Work out why the divisor reached zero and fix it at the source; \
            blindly clamping at the division site leaves the real bug in place.",
    },
    FaultClass {
        key: "unaligned_access",
        title: "Unaligned memory access",
        family: "memory",
        is_symptom: false,
        description: "A load/store broke the required alignment (Xtensa EXCCAUSE 9) — \
            commonly a cast to a wider type or a pointer into a packed struct.",
        remediation_hint: "Correct the pointer's alignment (memcpy through a buffer or use \
            a properly aligned type) rather than retrying the access.",
    },
    FaultClass {
        key: "illegal_instruction",
        title: "Illegal instruction",
        family: "control_flow",
        is_symptom: false,
        description: "The CPU decoded an invalid opcode (EXCCAUSE 0) with no stuck-unwinder \
            pattern — usually control flow jumped through a corrupted pointer into data.",
        remediation_hint: "Find what corrupted the function pointer or return target; the \
            bad jump is downstream of an overwrite.",
    },
    FaultClass {
        key: "bad_instruction_fetch",
        title: "Instruction fetch prohibited",
        family: "control_flow",
        is_symptom: false,
        description: "Execution landed in non-executable memory (EXCCAUSE 20) — a return \
            address or function pointer was overwritten and pointed into data or NULL.",
        remediation_hint: "Locate the overwrite (often a stack smash or use-after-free) that \
            clobbered the return address; fixing the jump target treats the wrong thing.",
    },
    FaultClass {
        key: "assert_failed",
        title: "Assertion / abort",
        family: "assertion",
        is_symptom: false,
        description: "The firmware trapped on purpose via assert()/abort(); the failed \
            expression states the invariant that broke.",
        remediation_hint: "Fix whatever violated the asserted invariant; deleting or \
            loosening the assert masks the symptom.",
    },
    FaultClass {
        key: "brownout",
        title: "Brownout reset",
        family: "power",
        is_symptom: false,
        description: "Supply voltage sagged below the brownout threshold — undersized \
            supply, a current spike (e.g. radio TX), or poor regulation/decoupling.",
        remediation_hint: "Fix the power delivery (supply headroom, decoupling, peak-current \
            budget). This is a hardware/power problem, not a firmware bug.",
    },
    FaultClass {
        key: "unknown",
        title: "Unknown",
        family: "unknown",
        is_symptom: false,
        description: "No hard signal matched a known fault class.",
        remediation_hint: "Collect more signal (a full backtrace, or the build ELF for \
            symbolization via `repair`) before attempting a fix.",
    },
];

/// FaultClass metadata for a crash class, falling back to 'unknown'.
pub fn taxonomy_for(crash_class: &str) -> &'static FaultClass {
    FAULT_TAXONOMY
        .iter()
        .find(|f| f.key == crash_class)
        .unwrap_or_else(|| {
            FAULT_TAXONOMY
                .iter()
                .find(|f| f.key == "unknown")
                .expect("taxonomy always has an 'unknown' entry")
        })
}

// ---------------------------------------------------------------------------
// Parsing — raw serial text → CrashFacts
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct CrashFacts {
    /// brownout | idf_watchdog | abort | guru_meditation | unknown
    pub panic_type: &'static str,
    pub exception_cause: Option<i64>,
    pub abort_reason: Option<String>,
    pub fault_pc: Option<String>,
    pub fault_addr: Option<String>,
    /// Insertion-ordered, like the Python dict it mirrors.
    pub registers: Vec<(String, String)>,
    pub backtrace: Vec<String>,
    pub task_name: Option<String>,
    pub cpu: Option<i64>,
}

impl Default for CrashFacts {
    fn default() -> Self {
        CrashFacts {
            panic_type: "unknown",
            exception_cause: None,
            abort_reason: None,
            fault_pc: None,
            fault_addr: None,
            registers: Vec::new(),
            backtrace: Vec::new(),
            task_name: None,
            cpu: None,
        }
    }
}

impl CrashFacts {
    #[cfg(test)]
    fn register(&self, name: &str) -> Option<&str> {
        self.registers
            .iter()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v.as_str())
    }
}

// Format detection. Order matters: a task-watchdog dump ends by calling abort()
// ("abort() was called"), so the watchdog signature must be checked before the abort
// one — while a bare abort()/assert with no watchdog text is its own root cause.
// Brownout output is unambiguous and checked first.
fn re_brownout() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(?i)Brownout detector was triggered").unwrap())
}

fn re_watchdog() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?i)Task watchdog got triggered|Interrupt wdt timeout|Guru Meditation Error.*?TWDT")
            .unwrap()
    })
}

fn re_abort() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(?i)abort\(\) was called|assert failed:").unwrap())
}

fn re_guru() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?is)Guru Meditation Error.*?Core\s+(\d+)\s+panic'ed").unwrap()
    })
}

// Field extraction (ESP-IDF panic-handler output shapes).
fn re_assert_msg() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"assert failed:\s*(.+)").unwrap())
}

fn re_abort_at() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?i)abort\(\) was called at PC\s*(0x[0-9a-fA-F]+)(?:\s+on core\s*(\d+))?")
            .unwrap()
    })
}

fn re_exccause() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"EXCCAUSE\s*[:=]\s*(0x[0-9a-fA-F]+|\d+)").unwrap())
}

fn re_excvaddr() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"EXCVADDR\s*[:=]\s*(0x[0-9a-fA-F]+)").unwrap())
}

fn re_pc() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\bPC\s*[:=]\s*(0x[0-9a-fA-F]+)").unwrap())
}

fn re_core() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"Core\s+(\d+)\s+panic'ed").unwrap())
}

fn re_register() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"\b(A\d{1,2}|PC|PS|SAR|EXCCAUSE|EXCVADDR)\s*[:=]\s*(0x[0-9a-fA-F]+)").unwrap()
    })
}

// "Backtrace: 0xPC:0xSP 0xPC:0xSP ..." — keep only the PC of each frame.
fn re_backtrace() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?i)Backtrace:\s*((?:0x[0-9a-fA-F]+(?::0x[0-9a-fA-F]+)?\s*)+)").unwrap()
    })
}

fn re_hex() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(0x[0-9a-fA-F]+)").unwrap())
}

fn re_task() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r#"(?i)Running task\s*['"]?([^\s'"]+)['"]?|Task\s*['"]([^'"]+)['"].*?watchdog"#)
            .unwrap()
    })
}

fn frames(raw: &str) -> Vec<String> {
    let Some(m) = re_backtrace().captures(raw) else {
        return Vec::new();
    };
    let mut frames = Vec::new();
    for token in m[1].split_whitespace() {
        if let Some(pc) = re_hex().captures(token) {
            frames.push(pc[1].to_string());
        }
    }
    frames
}

fn task(raw: &str) -> Option<String> {
    let m = re_task().captures(raw)?;
    m.get(1)
        .or_else(|| m.get(2))
        .map(|g| g.as_str().to_string())
}

/// Python `int(x, 0)`: parse "0x…" as hex, plain digits as decimal.
fn parse_int_auto(s: &str) -> Option<i64> {
    if let Some(hex) = s.strip_prefix("0x").or_else(|| s.strip_prefix("0X")) {
        i64::from_str_radix(hex, 16).ok()
    } else {
        s.parse().ok()
    }
}

/// Extract crash signals from raw serial text (format detected automatically).
pub fn parse_crash(raw: &str) -> CrashFacts {
    if re_brownout().is_match(raw) {
        return CrashFacts {
            panic_type: "brownout",
            ..Default::default()
        };
    }

    if re_watchdog().is_match(raw) {
        return CrashFacts {
            panic_type: "idf_watchdog",
            task_name: task(raw),
            ..Default::default()
        };
    }

    if re_abort().is_match(raw) {
        let mut facts = CrashFacts {
            panic_type: "abort",
            backtrace: frames(raw),
            ..Default::default()
        };
        if let Some(m) = re_assert_msg().captures(raw) {
            // The assert line names the violated invariant — the strongest signal here.
            facts.abort_reason = Some(format!("assert failed: {}", m[1].trim()));
        }
        if let Some(m) = re_abort_at().captures(raw) {
            facts.fault_pc = Some(m[1].to_string());
            if facts.abort_reason.is_none() {
                facts.abort_reason = Some(m[0].trim().to_string());
            }
            if let Some(core) = m.get(2) {
                facts.cpu = core.as_str().parse().ok();
            }
        }
        return facts;
    }

    if re_guru().is_match(raw) {
        let mut facts = CrashFacts {
            panic_type: "guru_meditation",
            backtrace: frames(raw),
            ..Default::default()
        };
        if let Some(m) = re_core().captures(raw) {
            facts.cpu = m[1].parse().ok();
        }
        if let Some(m) = re_exccause().captures(raw) {
            facts.exception_cause = parse_int_auto(&m[1]);
        }
        if let Some(m) = re_excvaddr().captures(raw) {
            facts.fault_addr = Some(m[1].to_string());
        }
        if let Some(m) = re_pc().captures(raw) {
            facts.fault_pc = Some(m[1].to_string());
        }
        for m in re_register().captures_iter(raw) {
            let (name, value) = (m[1].to_string(), m[2].to_string());
            match facts.registers.iter_mut().find(|(k, _)| *k == name) {
                Some(entry) => entry.1 = value,
                None => facts.registers.push((name, value)),
            }
        }
        facts.task_name = task(raw);
        return facts;
    }

    CrashFacts::default()
}

// ---------------------------------------------------------------------------
// Classification — CrashFacts → ClassificationResult (deterministic rules)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct Candidate {
    pub crash_class: &'static str,
    pub explanation: &'static str,
}

#[derive(Debug, Clone)]
pub struct ClassificationResult {
    pub crash_class: &'static str,
    pub confidence: f64,
    pub rationale: String,
    pub candidates: Vec<Candidate>,
}

impl ClassificationResult {
    fn new(crash_class: &'static str, confidence: f64, rationale: String) -> Self {
        ClassificationResult {
            crash_class,
            confidence,
            rationale,
            candidates: Vec::new(),
        }
    }

    fn with(mut self, candidates: Vec<Candidate>) -> Self {
        self.candidates = candidates;
        self
    }
}

// Xtensa EXCCAUSE values (Xtensa ISA exception causes).
const XT_ILLEGAL_INSTRUCTION: i64 = 0;
const XT_DIVIDE_BY_ZERO: i64 = 6;
const XT_LOAD_STORE_ALIGNMENT: i64 = 9;
const XT_INSTR_FETCH_PROHIBITED: i64 = 20;
const XT_LOAD_PROHIBITED: i64 = 28;
const XT_STORE_PROHIBITED: i64 = 29;

// Near-NULL window: an access below this is a NULL pointer plus a small struct offset.
const NULL_WINDOW: i64 = 0xFF;
// ESP32 DRAM heap region (approximate) — used only to flag heap-interior fault addresses.
const HEAP_LOW: i64 = 0x3FFB0000;
const HEAP_HIGH: i64 = 0x3FFF0000;

fn addr_int(value: Option<&str>) -> Option<i64> {
    let v = value?;
    let hex = v.strip_prefix("0x").or_else(|| v.strip_prefix("0X")).unwrap_or(v);
    i64::from_str_radix(hex, 16).ok()
}

fn near_null(value: Option<&str>) -> bool {
    matches!(addr_int(value), Some(v) if v <= NULL_WINDOW)
}

fn in_heap(value: Option<&str>) -> bool {
    matches!(addr_int(value), Some(v) if (HEAP_LOW..=HEAP_HIGH).contains(&v))
}

fn stuck_unwinder(backtrace: &[String], min_repeat: usize) -> bool {
    let mut counts: HashMap<&str, usize> = HashMap::new();
    for pc in backtrace {
        *counts.entry(pc.as_str()).or_insert(0) += 1;
    }
    counts.values().any(|&n| n >= min_repeat)
}

/// Render an optional value the way a Python f-string would ("None" when absent),
/// so rationale strings stay byte-identical with the reference implementation.
fn py_opt_str(value: Option<&str>) -> String {
    value.map(str::to_string).unwrap_or_else(|| "None".into())
}

fn py_opt_int(value: Option<i64>) -> String {
    value.map(|v| v.to_string()).unwrap_or_else(|| "None".into())
}

/// Rule-based classification from hard signals only. Pure — no I/O, no LLM.
pub fn classify(facts: &CrashFacts) -> ClassificationResult {
    let cause = facts.exception_cause;
    let addr = facts.fault_addr.as_deref();

    // Panic format alone is decisive for these three.
    if facts.panic_type == "idf_watchdog" {
        return ClassificationResult::new(
            "watchdog",
            0.97,
            "IDF watchdog format detected (no register dump) — a task starved the CPU.".into(),
        );
    }
    if facts.panic_type == "abort" {
        let reason = facts
            .abort_reason
            .as_deref()
            .unwrap_or("abort() was called");
        return ClassificationResult::new(
            "assert_failed",
            0.95,
            format!("Deliberate trap via abort()/assert(): {reason}"),
        );
    }
    if facts.panic_type == "brownout" {
        return ClassificationResult::new(
            "brownout",
            0.96,
            "Brownout detector fired — supply voltage dropped below threshold.".into(),
        );
    }

    // Prohibited store/load at a near-NULL address is a textbook null dereference.
    if cause == Some(XT_STORE_PROHIBITED) && near_null(addr) {
        return ClassificationResult::new(
            "null_deref",
            0.96,
            format!(
                "StoreProhibited (EXCCAUSE=29) writing near-NULL address {}.",
                py_opt_str(addr)
            ),
        );
    }
    if cause == Some(XT_STORE_PROHIBITED) {
        return ClassificationResult::new(
            "null_deref",
            0.75,
            format!(
                "StoreProhibited (EXCCAUSE=29) at {}; not near NULL — could be a \
                 misaligned or corrupted pointer write.",
                py_opt_str(addr)
            ),
        )
        .with(vec![Candidate {
            crash_class: "heap_corruption",
            explanation: "A store fault away from NULL can also be a corrupted heap pointer.",
        }]);
    }
    if cause == Some(XT_LOAD_PROHIBITED) && near_null(addr) {
        return ClassificationResult::new(
            "null_deref",
            0.96,
            format!(
                "LoadProhibited (EXCCAUSE=28) reading near-NULL address {}.",
                py_opt_str(addr)
            ),
        );
    }

    // Unambiguous EXCCAUSE-keyed faults win over the shape-based rules below.
    if cause == Some(XT_DIVIDE_BY_ZERO) {
        return ClassificationResult::new(
            "divide_by_zero",
            0.97,
            "IntegerDivideByZero (EXCCAUSE=6): integer division/modulo by zero.".into(),
        );
    }
    if cause == Some(XT_LOAD_STORE_ALIGNMENT) {
        return ClassificationResult::new(
            "unaligned_access",
            0.95,
            format!(
                "LoadStoreAlignment (EXCCAUSE=9) at {}: access broke required alignment.",
                py_opt_str(addr)
            ),
        );
    }
    if cause == Some(XT_INSTR_FETCH_PROHIBITED) {
        return ClassificationResult::new(
            "bad_instruction_fetch",
            0.90,
            format!(
                "InstrFetchProhibited (EXCCAUSE=20) at PC {}: executing \
                 non-executable memory — a clobbered return address or function pointer.",
                py_opt_str(facts.fault_pc.as_deref())
            ),
        )
        .with(vec![Candidate {
            crash_class: "stack_overflow",
            explanation: "A smashed stack overwrites return addresses, landing fetches in data.",
        }]);
    }

    // A backtrace looping on the same PC means the unwinder is stuck on a corrupt
    // stack. Checked before address-range rules: overflow into DRAM produces a
    // heap-range fault_addr that would otherwise look like heap corruption.
    if stuck_unwinder(&facts.backtrace, 3) {
        let candidates = if in_heap(addr) {
            vec![Candidate {
                crash_class: "heap_corruption",
                explanation: "A corrupted heap link can also loop the unwinder.",
            }]
        } else {
            Vec::new()
        };
        return ClassificationResult::new(
            "stack_overflow",
            0.93,
            "Backtrace repeats the same PC 3+ times — unwinder stuck on a corrupt stack frame."
                .into(),
        )
        .with(candidates);
    }
    if cause == Some(XT_ILLEGAL_INSTRUCTION) && stuck_unwinder(&facts.backtrace, 2) {
        return ClassificationResult::new(
            "stack_overflow",
            0.85,
            "IllegalInstruction with repeated backtrace PCs — SP likely corrupted, \
             CPU executing stack data."
                .into(),
        );
    }
    if cause == Some(XT_ILLEGAL_INSTRUCTION) {
        return ClassificationResult::new(
            "illegal_instruction",
            0.80,
            "IllegalInstruction (EXCCAUSE=0): invalid opcode — likely a corrupted code \
             pointer or a jump into data."
                .into(),
        )
        .with(vec![Candidate {
            crash_class: "bad_instruction_fetch",
            explanation: "A corrupted call target can also fault as a prohibited fetch.",
        }]);
    }

    if cause == Some(XT_LOAD_PROHIBITED) && in_heap(addr) {
        return ClassificationResult::new(
            "heap_corruption",
            0.72,
            format!(
                "LoadProhibited (EXCCAUSE=28) at heap-interior address {}.",
                py_opt_str(addr)
            ),
        )
        .with(vec![Candidate {
            crash_class: "null_deref",
            explanation: "A heap-range address can also be NULL plus a large struct offset.",
        }]);
    }
    if cause == Some(XT_LOAD_PROHIBITED) {
        return ClassificationResult::new(
            "null_deref",
            0.60,
            format!(
                "LoadProhibited (EXCCAUSE=28) at {}; address in neither the NULL window \
                 nor the heap range.",
                py_opt_str(addr)
            ),
        )
        .with(vec![Candidate {
            crash_class: "heap_corruption",
            explanation: "An unclassifiable load fault may be a dangling or corrupted pointer.",
        }]);
    }

    ClassificationResult::new(
        "unknown",
        0.30,
        format!(
            "No hard signal matched (panic_type='{}', EXCCAUSE={}, fault_addr={}).",
            facts.panic_type,
            py_opt_int(cause),
            py_opt_str(addr)
        ),
    )
    .with(vec![Candidate {
        crash_class: "null_deref",
        explanation: "A null-pointer access cannot be ruled out without a clear EXCCAUSE.",
    }])
}

// ---------------------------------------------------------------------------
// Entry point — raw serial text → structured-facts JSON
// ---------------------------------------------------------------------------

const EXCERPT_LINES: usize = 60;

fn excerpt(raw: &str) -> String {
    raw.lines().take(EXCERPT_LINES).collect::<Vec<_>>().join("\n")
}

/// Parse + classify a crash. Returns structured facts only — never a narrative.
///
/// When no panic signature is found the result is an honest `{"ok": false, ...}`;
/// a classification is never fabricated from unrecognized text.
pub fn diagnose(raw: &str) -> Value {
    let facts = parse_crash(raw);
    if facts.panic_type == "unknown" {
        return json!({
            "ok": false,
            "engine": ENGINE,
            "error": "no ESP32 panic signature found in input",
            "raw_excerpt": excerpt(raw),
        });
    }

    let result = classify(&facts);
    let meta = taxonomy_for(result.crash_class);
    let registers: Map<String, Value> = facts
        .registers
        .iter()
        .map(|(k, v)| (k.clone(), Value::String(v.clone())))
        .collect();
    json!({
        "ok": true,
        "engine": ENGINE,
        "crash_class": result.crash_class,
        "title": meta.title,
        "family": meta.family,
        "is_symptom": meta.is_symptom,
        "confidence": result.confidence,
        "rationale": result.rationale,
        "candidates": result.candidates.iter().map(|c| json!({
            "crash_class": c.crash_class,
            "explanation": c.explanation,
        })).collect::<Vec<_>>(),
        "description": meta.description,
        "remediation_hint": meta.remediation_hint,
        "panic_type": facts.panic_type,
        "exception_cause": facts.exception_cause,
        "fault_pc": facts.fault_pc,
        "fault_addr": facts.fault_addr,
        "registers": registers,
        "backtrace": facts.backtrace,
        "task_name": facts.task_name,
        "cpu": facts.cpu,
        "abort_reason": facts.abort_reason,
        "raw_excerpt": excerpt(raw),
        "note": "addresses are unsymbolized; `repair` (platform login) resolves them \
                 to function/file/line using the uploaded build ELF",
    })
}

// ---------------------------------------------------------------------------
// Tests — fixtures shared verbatim with the Python tests/test_diagnose.py;
// they are the behavioral parity oracle between the two implementations.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    const GURU_NULL_STORE: &str = r#"Guru Meditation Error: Core  1 panic'ed (StoreProhibited). Exception was unhandled.

Core  1 register dump:
PC      : 0x400d129c  PS      : 0x00060330  A0      : 0x800d2f10  A1      : 0x3ffb21b0
A2      : 0x00000000  A3      : 0x3ffb8058  A4      : 0x0000002a  A5      : 0x00000001
EXCCAUSE: 0x0000001d  EXCVADDR: 0x00000000  LBEG    : 0x4000c2e0  LEND    : 0x4000c2f6

Backtrace: 0x400d129c:0x3ffb21b0 0x400d2f0d:0x3ffb21d0 0x40086549:0x3ffb21f0
"#;

    const GURU_STACK_OVERFLOW: &str = r#"Guru Meditation Error: Core  0 panic'ed (LoadProhibited). Exception was unhandled.

Core  0 register dump:
PC      : 0x400d0d67  PS      : 0x00060c30  A0      : 0x800d0d67  A1      : 0x3ffb1fb0
EXCCAUSE: 0x0000001c  EXCVADDR: 0x3ffb3ff0

Backtrace: 0x400d0d67:0x3ffb1fb0 0x400d0d67:0x3ffb1fd0 0x400d0d67:0x3ffb1ff0 0x400d0d67:0x3ffb2010
"#;

    const TASK_WATCHDOG: &str = r#"E (10314) task_wdt: Task watchdog got triggered. The following tasks did not reset the watchdog in time:
E (10314) task_wdt:  - IDLE (CPU 0)
E (10314) task_wdt: Tasks currently running:
E (10314) task_wdt: Running task 'sensor_poll'
abort() was called at PC 0x40084d0d on core 0
"#;

    const ASSERT_FAILED: &str = r#"assert failed: xQueueSemaphoreTake queue.c:1549 (( pxQueue ))

abort() was called at PC 0x40088343 on core 1

Backtrace: 0x40088343:0x3ffb1e10 0x400d10bd:0x3ffb1e30
"#;

    const BROWNOUT: &str = r#"Brownout detector was triggered

ets Jul 29 2019 12:21:46

rst:0xc (SW_CPU_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)
"#;

    const GURU_DIV_ZERO: &str = r#"Guru Meditation Error: Core  1 panic'ed (IntegerDivideByZero). Exception was unhandled.
PC      : 0x400d1010  PS      : 0x00060330
EXCCAUSE: 0x00000006  EXCVADDR: 0x00000000
Backtrace: 0x400d1010:0x3ffb21b0 0x400d2f0d:0x3ffb21d0
"#;

    const GURU_UNALIGNED: &str = r#"Guru Meditation Error: Core  0 panic'ed (LoadStoreAlignment). Exception was unhandled.
PC      : 0x400d2222  PS      : 0x00060330
EXCCAUSE: 0x00000009  EXCVADDR: 0x3ffb8001
Backtrace: 0x400d2222:0x3ffb21b0
"#;

    const GURU_BAD_FETCH: &str = r#"Guru Meditation Error: Core  0 panic'ed (InstrFetchProhibited). Exception was unhandled.
PC      : 0x00000000  PS      : 0x00060330
EXCCAUSE: 0x00000014  EXCVADDR: 0x00000000
Backtrace: 0x00000000:0x3ffb21b0 0x400d2f0d:0x3ffb21d0
"#;

    const GARBAGE: &str = "hello world\nnormal boot log\nWiFi connected, IP 192.168.1.50\n";

    // ── parser ──────────────────────────────────────────────────────────────

    #[test]
    fn parse_guru_extracts_signals() {
        let facts = parse_crash(GURU_NULL_STORE);
        assert_eq!(facts.panic_type, "guru_meditation");
        assert_eq!(facts.exception_cause, Some(29));
        assert_eq!(facts.fault_addr.as_deref(), Some("0x00000000"));
        assert_eq!(facts.fault_pc.as_deref(), Some("0x400d129c"));
        assert_eq!(facts.cpu, Some(1));
        assert_eq!(facts.register("A2"), Some("0x00000000"));
        assert_eq!(
            facts.backtrace,
            vec!["0x400d129c", "0x400d2f0d", "0x40086549"]
        );
    }

    #[test]
    fn parse_watchdog_beats_trailing_abort() {
        // A task-watchdog dump ends in abort(); it must still parse as a watchdog.
        let facts = parse_crash(TASK_WATCHDOG);
        assert_eq!(facts.panic_type, "idf_watchdog");
        assert_eq!(facts.task_name.as_deref(), Some("sensor_poll"));
    }

    #[test]
    fn parse_abort_prefers_assert_message() {
        let facts = parse_crash(ASSERT_FAILED);
        assert_eq!(facts.panic_type, "abort");
        assert!(facts
            .abort_reason
            .as_deref()
            .unwrap()
            .starts_with("assert failed: xQueueSemaphoreTake"));
        assert_eq!(facts.fault_pc.as_deref(), Some("0x40088343"));
        assert_eq!(facts.cpu, Some(1));
    }

    #[test]
    fn parse_unknown_on_clean_log() {
        assert_eq!(parse_crash(GARBAGE).panic_type, "unknown");
    }

    // ── classifier (diagnose = parse + classify + taxonomy) ─────────────────

    #[test]
    fn null_deref_high_confidence() {
        let out = diagnose(GURU_NULL_STORE);
        assert_eq!(out["ok"], true);
        assert_eq!(out["crash_class"], "null_deref");
        assert_eq!(out["confidence"], 0.96);
        assert_eq!(out["family"], "memory");
        assert_eq!(out["is_symptom"], false);
    }

    #[test]
    fn stack_overflow_from_repeated_pcs() {
        let out = diagnose(GURU_STACK_OVERFLOW);
        assert_eq!(out["crash_class"], "stack_overflow");
        assert_eq!(out["confidence"], 0.93);
        // heap-range EXCVADDR keeps heap_corruption as an honest alternative
        assert!(out["candidates"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["crash_class"] == "heap_corruption"));
    }

    #[test]
    fn watchdog_is_symptom_not_abort() {
        let out = diagnose(TASK_WATCHDOG);
        assert_eq!(out["crash_class"], "watchdog");
        assert_eq!(out["is_symptom"], true);
        assert_eq!(out["family"], "timing");
    }

    #[test]
    fn assert_failed_class() {
        let out = diagnose(ASSERT_FAILED);
        assert_eq!(out["crash_class"], "assert_failed");
        assert!(out["rationale"]
            .as_str()
            .unwrap()
            .contains("xQueueSemaphoreTake"));
    }

    #[test]
    fn brownout_class() {
        let out = diagnose(BROWNOUT);
        assert_eq!(out["crash_class"], "brownout");
        assert_eq!(out["family"], "power");
    }

    #[test]
    fn exccause_keyed_classes() {
        assert_eq!(diagnose(GURU_DIV_ZERO)["crash_class"], "divide_by_zero");
        assert_eq!(diagnose(GURU_UNALIGNED)["crash_class"], "unaligned_access");
        let out = diagnose(GURU_BAD_FETCH);
        assert_eq!(out["crash_class"], "bad_instruction_fetch");
        assert!(out["candidates"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["crash_class"] == "stack_overflow"));
    }

    #[test]
    fn no_panic_is_honest_not_fabricated() {
        let out = diagnose(GARBAGE);
        assert_eq!(out["ok"], false);
        assert!(out["error"]
            .as_str()
            .unwrap()
            .contains("no ESP32 panic signature"));
        assert!(out.get("crash_class").is_none());
    }

    #[test]
    fn empty_input() {
        assert_eq!(diagnose("")["ok"], false);
    }

    #[test]
    fn taxonomy_covers_all_classes() {
        let keys: std::collections::HashSet<&str> =
            FAULT_TAXONOMY.iter().map(|f| f.key).collect();
        let expected: std::collections::HashSet<&str> = [
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
        .into_iter()
        .collect();
        assert_eq!(keys, expected);
        assert_eq!(taxonomy_for("nonsense").key, "unknown");
    }

    #[test]
    fn only_watchdog_is_a_symptom() {
        for f in FAULT_TAXONOMY {
            assert_eq!(f.is_symptom, f.key == "watchdog", "class {}", f.key);
        }
    }
}
