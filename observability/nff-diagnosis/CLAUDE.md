# CLAUDE.md — nff-maintenance

Build spec for Claude Code. Read this fully before writing any code. It encodes
decisions that are already made — do not re-litigate them, do not "improve" the
scope, and do not build anything in the **Out of scope** list.

---

## What this is

`nff-maintenance` is the **diagnosis layer** of nff. It takes a crash that came off 
an embedded device and produces a specific, source-level explanation of *why* the
device failed (e.g. "stack overflow in `poll_sensors()` at sensors.c:88").

It sits beside the open-source `nff` bridge (hardware ↔ Claude Code over USB).
`nff` flashes boards and reads serial. **This repo does not reimplement that** — it
shells out to the `nff` CLI for anything device-facing.

**This repo is NOT open source. Keep it in a separate repo from `nff`. Never copy
code from here into the MIT-licensed `nff` repo.** The bridge is the funnel; this
is the business.

---

## The validated core (do not re-test, just build on it)

A bench test confirmed an LLM produces specific, correct diagnoses from raw ESP32
crash output: it distinguishes a null-pointer write (StoreProhibited + EXCVADDR 0x0),
a stack overflow (repeated-PC backtrace + canary watchpoint), and a watchdog timeout
(IDF task-list format, no register dump). The signal in a panic dump is rich enough.
This repo turns that into a repeatable service.

---

## Architecture (one engine, two ingestion paths)

A server-side **service** holds the diagnosis engine and an ELF store. Crashes arrive
from two kinds of client; the engine treats them identically.

```
  BENCH CLIENT (build now)          FIELD CLIENT (build later)
  bench CLI on dev PC               deployed IoT device
  - flashes via `nff`               - runs firmware, crashes
  - captures panic off serial       - reports logs home
  - uploads ELF + build_id          - sends crash + build_id
  - sends crash + build_id                   |
            |                                 |
            +-------------> INGESTION ENDPOINT <-----------+
                                  |
                                  v
            CRASH RECORD (carries build_id)
                                  |
              [ELF STORE: build_id -> ELF]  <-- looked up here
                                  |
                                  v
            SYMBOL RESOLUTION (addr2line, server-side)
                                  |
                                  v
            DIAGNOSIS ENGINE (the moat)
                                  |
                                  v
            STORAGE (crash records + diagnoses) + thin view
```

Key invariants:
- **`build_id` is the join key across everything.** It is the hash of the ELF.
  It is embedded in the firmware at build time and travels back with every crash.
- **The ELF lives in the cloud store, never sent by field devices.** The bench client
  *populates* the store at flash time; field crashes *read* from it via `build_id`.
- **Symbol resolution happens server-side in BOTH modes** — one code path, never two.
  On bench the ELF is also uploaded; the engine still resolves server-side.
- **The engine is source-agnostic.** It does not know or care whether a crash came
  from a USB cable or a cellular device. Same input schema, same path.

---

## The three schemas (the spine — define these FIRST, in code, before any tool)

`build_id` has the same name, type, and format in all three. It is a hash of the ELF.

### 1. ELF-upload contract (bench flow -> ELF store, at flash time)
- `build_id` — hash of the ELF (the key)
- `elf` — the ELF blob (or a storage reference if uploaded separately)
- `target` — chip/arch, e.g. "esp32" / xtensa (reserved for multi-arch later)
- `firmware_version` — human-friendly name/version
- `project` / `customer` — isolation key (per-customer separation)
- `toolchain` — toolchain/addr2line version used (saves future debugging)
- `created_at`

### 2. Crash record (either client -> ingestion endpoint)
- `crash_id` — unique id for this crash
- `build_id` — join key; without it, resolution is impossible
- `device_id` — null/trivial on bench, meaningful in field (reserve now)
- `panic_type` — format/class of panic (Guru Meditation vs IDF watchdog log;
  the format alone separates WDT from memory faults)
- `exception_cause` — e.g. EXCCAUSE value
- `fault_pc` — faulting PC
- `fault_addr` — EXCVADDR
- `registers` — map of register name -> value
- `backtrace` — list of raw PC addresses (unresolved)
- `task_name`, `cpu` — when present
- `core_dump` — optional blob
- `raw` — the original captured serial/text, ALWAYS kept even though parsed.
  When the parser misses something, re-parse `raw` instead of re-crashing a board.
- `received_at`

### 3. Diagnosis output (engine -> storage / view)
- `crash_id` — the crash this explains (reference; crash is always stored too)
- `crash_class` — the generalized fault taxonomy (10 real classes + `unknown`):
  null_deref | stack_overflow | watchdog | heap_corruption | divide_by_zero |
  unaligned_access | illegal_instruction | bad_instruction_fetch | assert_failed |
  brownout | unknown. Single source of truth: `schemas/diagnosis_output.py::CrashClass`;
  metadata (family, is_symptom, remediation) in `schemas/taxonomy.py::FAULT_TAXONOMY`.
  Adding a root cause = one FAULT_TAXONOMY entry + one classifier rule keyed on a HARD
  signal (see the accuracy bench: `nff-diag eval taxonomy`).
- `family` — coarse grouping (memory | control_flow | arithmetic | assertion | timing |
  power | unknown), from the taxonomy registry.
- `is_symptom` — True when this class is a symptom layer that usually masks a deeper root
  cause (e.g. `watchdog` — the root cause is whatever blocked). Lets consumers score a
  root-cause-correct fix vs a symptom mask.
- `root_cause` — plain-language explanation
- `location` — resolved `function` + `file` + `line` of the relevant frame
- `resolved_backtrace` — each PC mapped to function/file/line
- `confidence` — REQUIRED. Forces honest uncertainty at the schema level.
- `candidates` — REQUIRED when ambiguous: list of alternative causes.
  A confident-but-wrong diagnosis is worse than an honest "unsure" — the schema
  must make uncertainty expressible, not optional.
- `engine_version` — which engine/model produced this
- `created_at`

---

## Build order (CLI-first; each step independently runnable and testable)

Do these in order. Do not start a step before the previous one runs end-to-end.
Everything communicates via CLI and structured JSON at the boundaries. MCP comes
LATER as a thin wrapper over the proven CLIs — do not build MCP now.

1. **Schemas in code.** Define the three structures above as the single source of
   truth (typed models). Every tool speaks these. Nothing else is written first.

2. **ELF store + upload.** A command that, given an ELF, computes `build_id` (hash),
   stores the ELF keyed by `build_id` with its metadata, and can retrieve it later.
   Start with a local folder/store; design the interface so a cloud backend swaps in
   without touching callers.

3. **Bench capture CLI.** A command that flashes via the `nff` CLI, captures the
   panic off serial (shell out to `nff` — do not reimplement serial), parses it into
   a **crash record** (with `build_id`), and uploads the ELF to the store. Must keep
   `raw`. Emits crash-record JSON.

4. **Symbol resolution tool.** Crash record + ELF (fetched from store by `build_id`)
   in -> enriched record with `resolved_backtrace` and `location` out, via `addr2line`
   (`xtensa-esp32-elf-addr2line`). Handle: missing ELF, optimized-away/inlined frames,
   Xtensa PC-minus-3 convention. Degrade gracefully and SAY SO when ELF is missing.

5. **Diagnosis engine.** Enriched record in -> diagnosis JSON out. Classify from HARD
   signals (exception cause, panic format, backtrace shape) — encode the validated
   signatures, do not guess. Populate `confidence` and `candidates` honestly.

6. **Ingestion endpoint + storage.** One HTTP endpoint that accepts a crash record
   (carrying `build_id`), runs resolution -> engine, stores crash + diagnosis. The
   bench CLI is its FIRST client. A field device is just another client later.

7. **Thin view.** Minimal surface to read a diagnosis alongside its crash. Nothing more.

Then, and only then: MCP wrapper over these CLIs.

---

## Out of scope (DO NOT BUILD — each is a known rabbit hole)

If you find yourself building any of these, STOP — you've drifted off the plan.

- **On-device reporting agent / phone-home firmware.** The field client is later.
  The bench CLI is the first and only ingestion client for now.
- **MCP layer.** Build CLIs first; MCP wraps them later.
- **Recovery image, A/B partitions, rollback.** Commodity (ESP-IDF/MCUboot). Not here.
- **OTA / firmware update mechanism.** Incumbent territory. Not here.
- **Remote-while-dead / out-of-band recovery.** Needs hardware/probe. Research, later.
- **Delta updates / patch "stitcher".** Saves bandwidth not flash; not a moat.
- **Dashboards, fleet management, multi-user accounts, polished UI.**
- **A second chip architecture.** ESP32/Xtensa only until it's proven and paid for.
  (But keep the `target` field so ARM/STM32 bolts on later without a rewrite.)

---

## Engineering rules

- **JSON (or typed objects) at every tool boundary.** No loose text-parsing between
  layers. This is what keeps a CLI-composed system from rotting and makes the MCP
  wrapper trivial later.
- **Shell out to `nff` for all device I/O.** Do not import or fork it; call its CLI.
- **Local first, cloud-swappable.** Build the ELF store and ingestion to run locally,
  behind interfaces that let a cloud backend replace them without changing callers.
- **Per-customer isolation in the store from the start** — don't architect a single
  shared namespace you can't later separate. ELFs are sensitive customer IP.
- **Keep `raw` crash text always.** Re-parse it; don't re-crash boards.
- **Confidence is required output.** The engine must be able to say "I'm not sure"
  and list candidates. Never ship a path that is always confident.

---

## Validation gates (before calling the engine shippable)

The bench test used three textbook bugs written on purpose. Before this is a product,
the diagnosis must survive harder tests:
1. **Unfamiliar firmware** — a crash in a project not written here, no priming.
2. **Ambiguous crash** — heap corruption, or a bug manifesting away from its root
   cause; check the diagnosis is honest, not confidently wrong.
3. **Consistency** — same crash several times; the diagnosis must not drift.

If diagnosis degrades to plausible-but-wrong on these, fix that before shipping.
That failure mode is the single biggest product risk.

---

## Target hardware (first cut)

ESP32 (Xtensa), where the bench validation happened. Structure crash-parsing and
symbol resolution so a second architecture can be added later via the `target` field
— but do not build the second architecture until the first is proven and paid for.