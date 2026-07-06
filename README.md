# nff-diagnosis

Diagnosis layer for ESP32 embedded device crashes. Takes a raw panic dump off a device and produces a specific, source-level explanation of why it failed — function name, file, line number, root cause.

Sits beside the [`nff`](https://github.com/GLechevalier/nff) bridge (hardware ↔ Claude Code over USB). `nff` flashes boards and reads serial. This repo does not reimplement that — it shells out to the `nff` CLI for all device-facing I/O.

---

## What it does

```
ESP32 panic dump
       │
       ▼
  crash parser          (extracts EXCCAUSE, EXCVADDR, backtrace PCs, registers)
       │
       ▼
  symbol resolution     (xtensa-esp32-elf-addr2line maps PCs → function/file/line)
       │
       ▼
  rule classifier       (hard signals → crash_class + confidence)
       │
       ▼
  Claude API narrative  (one-paragraph root cause, grounded in the evidence)
       │
       ▼
  DiagnosisOutput JSON  (crash_class, root_cause, location, resolved_backtrace, confidence)
```

Validated against three textbook ESP32 bugs: null-pointer write (StoreProhibited + EXCVADDR 0x0), stack overflow (repeated-PC backtrace + canary watchpoint), and watchdog timeout (IDF task-list format). The signal in a panic dump is rich enough for confident, specific diagnosis.

---

## Architecture

Two ingestion paths, one engine:

```
  BENCH (dev PC)                    FIELD (IoT device, later)
  flash via nff CLI                 firmware crashes, reports home
  capture serial panic              sends crash + build_id
  upload ELF + build_id                      │
            │                                │
            └──────► POST /ingest ◄──────────┘
                           │
                    crash record
                           │
                  [ELF store: build_id → ELF]
                           │
                   addr2line resolution
                           │
                    diagnosis engine
                           │
                    storage + view
```

`build_id` is the SHA-256 of the ELF. It is the join key across everything — embedded in firmware at build time, sent with every crash, used to look up the ELF for symbol resolution.

---

## Installation

Requires Python 3.11+. Uses the `.venv` from the parent `nff_cli_mcp` workspace, or create your own:

```bash
python -m pip install -e ".[dev]"
```

For symbol resolution you also need the Xtensa toolchain:

```bash
# macOS (Homebrew)
brew install esptool xtensa-esp32-elf

# Or install via ESP-IDF: https://docs.espressif.com/projects/esp-idf/en/latest/esp32/get-started/
```

Set your Anthropic API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

---

## Quick start (no hardware)

```bash
# 1. Parse a saved panic dump
nff-diag capture from-file \
  --raw tests/fixtures/crash_null_deref.txt \
  --build-id abc123 \
  > /tmp/crash.json

# 2. Diagnose it (calls Claude API)
nff-diag diagnose /tmp/crash.json

# 3. Pretty-print via the view commands (after a server ingest)
nff-diag server start --port 8080 &
curl -s -X POST http://localhost:8080/ingest \
  -H "Content-Type: application/json" \
  -d @/tmp/crash.json | python -m json.tool
```

---

## Full bench workflow (with hardware)

```bash
# Upload the ELF to the local store at flash time
nff-diag elf upload \
  --elf firmware/build/sensor_node.elf \
  --customer acme \
  --project sensor-node \
  --firmware-version 1.2.0

# Flash, capture, parse, and emit CrashRecord JSON in one step
nff-diag capture flash-and-capture \
  --elf firmware/build/sensor_node.elf \
  --port COM3 \
  --board esp32:esp32:esp32 \
  --customer acme \
  --project sensor-node \
  --firmware-version 1.2.0 \
  > crash.json

# Resolve symbols + diagnose
nff-diag diagnose crash.json

# View stored results
nff-diag view list
nff-diag view diag <crash_id> --pretty
```

---

## CLI reference

```
nff-diag [--store-root DIR] [--db PATH]
├── elf
│   ├── upload --elf PATH --customer X --project Y --firmware-version V
│   └── show BUILD_ID
│
├── capture
│   ├── flash-and-capture --elf PATH --port PORT --board FQBN
│   │                     --customer X --project Y --firmware-version V
│   └── from-file --raw PATH --build-id BUILD_ID
│
├── resolve CRASH_JSON_OR_FILE     # resolve backtrace, no diagnosis
├── diagnose CRASH_JSON_OR_FILE    # resolve + diagnose (calls Claude API)
│
├── server
│   └── start [--host 127.0.0.1] [--port 8080]
│
└── view
    ├── crash CRASH_ID [--pretty]
    ├── diag  CRASH_ID [--pretty]
    └── list  [--limit 50]
```

Pass `-` as the JSON argument to read from stdin. All commands emit JSON to stdout and `ERROR: ...` to stderr.

**Global defaults:**
- `--store-root` → `~/.nff-diagnosis/store` (override with `NFF_STORE_ROOT`)
- `--db` → `~/.nff-diagnosis/crashes.db` (override with `NFF_DB_PATH`)

---

## HTTP API

Start the server with `nff-diag server start`, then:

```
POST /ingest
  Body:    CrashRecord JSON
  Returns: DiagnosisOutput JSON (201)
```

The bench CLI is the first client. A deployed IoT device is just another client later — same endpoint, same schema.

---

## Schemas

Three Pydantic v2 models are the spine of the system. `build_id` has the same type and format in all three.

**`ELFRecord`** — stored at flash time, keyed by `build_id` (SHA-256 of ELF bytes).

**`CrashRecord`** — emitted by the capture CLI or sent by a device. Always carries `raw` (the original serial text) so crashes can be re-parsed without re-crashing hardware.

**`DiagnosisOutput`** — always includes `confidence` (0.0–1.0) and `candidates` (alternative explanations when ambiguous). A confident-but-wrong diagnosis is worse than an honest "unsure". It also carries `family` and `is_symptom` from the taxonomy registry so consumers can tell a root-cause-correct fix from a symptom mask.

```
crash_class: null_deref | stack_overflow | watchdog | heap_corruption
           | divide_by_zero | unaligned_access | illegal_instruction
           | bad_instruction_fetch | assert_failed | brownout | unknown
family:      memory | control_flow | arithmetic | assertion | timing | power | unknown
is_symptom:  true for classes that mask a deeper cause (e.g. watchdog)
```

Score the taxonomy on a labelled corpus offline (no LLM, no hardware):

```
nff-diag eval taxonomy      # confusion matrix + per-class accuracy; exits non-zero on a miss
```

---

## Diagnosis engine

Classification is rule-based from hard signals (exception cause value, fault address range, backtrace shape) — no guessing. Claude then writes the human-readable root cause paragraph, grounded in the classified evidence.

| Signal | Classification | Confidence |
|---|---|---|
| IDF watchdog task-list format | watchdog | 0.97 |
| EXCCAUSE=28 + EXCVADDR ≤ 0xFF | null_deref | 0.96 |
| EXCCAUSE=3 + EXCVADDR ≤ 0xFF | null_deref | 0.96 |
| EXCCAUSE=3 + EXCVADDR in heap range | heap_corruption | 0.72 |
| 3+ identical PCs in backtrace | stack_overflow | 0.93 |
| No matching signal | unknown | 0.30 |

When `confidence < 0.80`, `candidates` is always populated with at least one alternative.

---

## Tests

```bash
pytest tests/ -v
```

36 tests, no hardware required. The diagnosis engine and Claude API are monkeypatched. Four fixture crash dumps cover the validated failure modes:

| Fixture | Type | Key signal |
|---|---|---|
| `crash_null_deref.txt` | StoreProhibited | EXCCAUSE=28, EXCVADDR=0x0 |
| `crash_stack_overflow.txt` | Debug exception | 5× identical PC in backtrace |
| `crash_watchdog.txt` | IDF WDT | task-list format, no register dump |
| `crash_heap_corrupt.txt` | LoadProhibited | EXCVADDR in heap interior |

---

## Project layout

```
nff_diagnosis/
├── schemas/          # ELFRecord, CrashRecord, DiagnosisOutput (Pydantic v2)
├── store/            # ELF store: base ABC + local filesystem impl
├── storage/          # Crash+diagnosis storage: base ABC + SQLite impl
├── capture/          # ESP32PanicParser + NffBridge (shells out to nff CLI)
├── resolution/       # Addr2LineResolver (xtensa-esp32-elf-addr2line)
├── engine/           # RuleClassifier + DiagnosisEngine (Claude API)
├── server/           # FastAPI app, POST /ingest, pipeline orchestrator
└── cli/              # Click commands (nff-diag entry point)
tests/
└── fixtures/         # Verbatim ESP32 serial panic dumps
```

The store and storage layers are behind abstract base classes. Swapping in a cloud backend (S3 for ELFs, Postgres for records) means implementing the ABC — no caller code changes.

---

## Out of scope

Per the build spec — do not build these:

- On-device phone-home / field client (bench CLI is the only ingestion client for now)
- MCP layer (CLIs come first; MCP wraps them later)
- OTA, A/B partitions, recovery images
- Dashboards, fleet management, multi-user UI
- Second chip architecture (ESP32/Xtensa only until proven and paid for)
