"""Tests for nff.tools.diagnose — local crash parsing + rule classification.

The fixtures are realistic ESP-IDF panic dumps, one per fault family. They double as
the behavioral parity oracle for the server-side diagnosis engine.
"""

import json

from click.testing import CliRunner

from nff.tools import diagnose as dg

# ---------------------------------------------------------------------------
# Golden fixtures (shapes from the ESP-IDF panic handler)
# ---------------------------------------------------------------------------

GURU_NULL_STORE = """\
Guru Meditation Error: Core  1 panic'ed (StoreProhibited). Exception was unhandled.

Core  1 register dump:
PC      : 0x400d129c  PS      : 0x00060330  A0      : 0x800d2f10  A1      : 0x3ffb21b0
A2      : 0x00000000  A3      : 0x3ffb8058  A4      : 0x0000002a  A5      : 0x00000001
EXCCAUSE: 0x0000001d  EXCVADDR: 0x00000000  LBEG    : 0x4000c2e0  LEND    : 0x4000c2f6

Backtrace: 0x400d129c:0x3ffb21b0 0x400d2f0d:0x3ffb21d0 0x40086549:0x3ffb21f0
"""

GURU_STACK_OVERFLOW = """\
Guru Meditation Error: Core  0 panic'ed (LoadProhibited). Exception was unhandled.

Core  0 register dump:
PC      : 0x400d0d67  PS      : 0x00060c30  A0      : 0x800d0d67  A1      : 0x3ffb1fb0
EXCCAUSE: 0x0000001c  EXCVADDR: 0x3ffb3ff0

Backtrace: 0x400d0d67:0x3ffb1fb0 0x400d0d67:0x3ffb1fd0 0x400d0d67:0x3ffb1ff0 0x400d0d67:0x3ffb2010
"""

TASK_WATCHDOG = """\
E (10314) task_wdt: Task watchdog got triggered. The following tasks did not reset the watchdog in time:
E (10314) task_wdt:  - IDLE (CPU 0)
E (10314) task_wdt: Tasks currently running:
E (10314) task_wdt: Running task 'sensor_poll'
abort() was called at PC 0x40084d0d on core 0
"""

ASSERT_FAILED = """\
assert failed: xQueueSemaphoreTake queue.c:1549 (( pxQueue ))

abort() was called at PC 0x40088343 on core 1

Backtrace: 0x40088343:0x3ffb1e10 0x400d10bd:0x3ffb1e30
"""

BROWNOUT = """\
Brownout detector was triggered

ets Jul 29 2019 12:21:46

rst:0xc (SW_CPU_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)
"""

GURU_DIV_ZERO = """\
Guru Meditation Error: Core  1 panic'ed (IntegerDivideByZero). Exception was unhandled.
PC      : 0x400d1010  PS      : 0x00060330
EXCCAUSE: 0x00000006  EXCVADDR: 0x00000000
Backtrace: 0x400d1010:0x3ffb21b0 0x400d2f0d:0x3ffb21d0
"""

GURU_UNALIGNED = """\
Guru Meditation Error: Core  0 panic'ed (LoadStoreAlignment). Exception was unhandled.
PC      : 0x400d2222  PS      : 0x00060330
EXCCAUSE: 0x00000009  EXCVADDR: 0x3ffb8001
Backtrace: 0x400d2222:0x3ffb21b0
"""

GURU_BAD_FETCH = """\
Guru Meditation Error: Core  0 panic'ed (InstrFetchProhibited). Exception was unhandled.
PC      : 0x00000000  PS      : 0x00060330
EXCCAUSE: 0x00000014  EXCVADDR: 0x00000000
Backtrace: 0x00000000:0x3ffb21b0 0x400d2f0d:0x3ffb21d0
"""

GARBAGE = "hello world\nnormal boot log\nWiFi connected, IP 192.168.1.50\n"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_guru_extracts_signals():
    facts = dg.parse_crash(GURU_NULL_STORE)
    assert facts.panic_type == "guru_meditation"
    assert facts.exception_cause == 29
    assert facts.fault_addr == "0x00000000"
    assert facts.fault_pc == "0x400d129c"
    assert facts.cpu == 1
    assert facts.registers["A2"] == "0x00000000"
    assert facts.backtrace == ["0x400d129c", "0x400d2f0d", "0x40086549"]


def test_parse_watchdog_beats_trailing_abort():
    # A task-watchdog dump ends in abort(); it must still parse as a watchdog.
    facts = dg.parse_crash(TASK_WATCHDOG)
    assert facts.panic_type == "idf_watchdog"
    assert facts.task_name == "sensor_poll"


def test_parse_abort_prefers_assert_message():
    facts = dg.parse_crash(ASSERT_FAILED)
    assert facts.panic_type == "abort"
    assert facts.abort_reason.startswith("assert failed: xQueueSemaphoreTake")
    assert facts.fault_pc == "0x40088343"
    assert facts.cpu == 1


def test_parse_unknown_on_clean_log():
    assert dg.parse_crash(GARBAGE).panic_type == "unknown"


# ---------------------------------------------------------------------------
# Classifier (diagnose = parse + classify + taxonomy)
# ---------------------------------------------------------------------------


def test_null_deref_high_confidence():
    out = dg.diagnose(GURU_NULL_STORE)
    assert out["ok"] is True
    assert out["crash_class"] == "null_deref"
    assert out["confidence"] == 0.96
    assert out["family"] == "memory"
    assert out["is_symptom"] is False


def test_stack_overflow_from_repeated_pcs():
    out = dg.diagnose(GURU_STACK_OVERFLOW)
    assert out["crash_class"] == "stack_overflow"
    assert out["confidence"] == 0.93
    # heap-range EXCVADDR keeps heap_corruption as an honest alternative
    assert any(c["crash_class"] == "heap_corruption" for c in out["candidates"])


def test_watchdog_is_symptom_not_abort():
    out = dg.diagnose(TASK_WATCHDOG)
    assert out["crash_class"] == "watchdog"
    assert out["is_symptom"] is True
    assert out["family"] == "timing"


def test_assert_failed():
    out = dg.diagnose(ASSERT_FAILED)
    assert out["crash_class"] == "assert_failed"
    assert "xQueueSemaphoreTake" in out["rationale"]


def test_brownout():
    out = dg.diagnose(BROWNOUT)
    assert out["crash_class"] == "brownout"
    assert out["family"] == "power"


def test_exccause_keyed_classes():
    assert dg.diagnose(GURU_DIV_ZERO)["crash_class"] == "divide_by_zero"
    assert dg.diagnose(GURU_UNALIGNED)["crash_class"] == "unaligned_access"
    out = dg.diagnose(GURU_BAD_FETCH)
    assert out["crash_class"] == "bad_instruction_fetch"
    assert any(c["crash_class"] == "stack_overflow" for c in out["candidates"])


def test_no_panic_is_honest_not_fabricated():
    out = dg.diagnose(GARBAGE)
    assert out["ok"] is False
    assert "no ESP32 panic signature" in out["error"]
    assert "crash_class" not in out


def test_empty_input():
    assert dg.diagnose("")["ok"] is False


def test_taxonomy_covers_all_classes():
    assert set(dg.FAULT_TAXONOMY) == {
        "null_deref", "stack_overflow", "watchdog", "heap_corruption", "divide_by_zero",
        "unaligned_access", "illegal_instruction", "bad_instruction_fetch",
        "assert_failed", "brownout", "unknown",
    }
    assert dg.taxonomy_for("nonsense").key == "unknown"


# ---------------------------------------------------------------------------
# CLI: nff diagnose
# ---------------------------------------------------------------------------


def test_cli_diagnose_json():
    from nff.commands.diagnose import diagnose as diagnose_cmd
    runner = CliRunner()
    result = runner.invoke(diagnose_cmd, ["--serial", GURU_NULL_STORE, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["crash_class"] == "null_deref"


def test_cli_diagnose_human_summary():
    from nff.commands.diagnose import diagnose as diagnose_cmd
    runner = CliRunner()
    result = runner.invoke(diagnose_cmd, ["--serial", TASK_WATCHDOG])
    assert result.exit_code == 0, result.output
    assert "watchdog" in result.output.lower()


def test_cli_diagnose_requires_input():
    from nff.commands.diagnose import diagnose as diagnose_cmd
    runner = CliRunner()
    result = runner.invoke(diagnose_cmd, [])
    assert result.exit_code != 0
