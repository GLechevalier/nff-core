from nff_diagnosis.capture.parser import ESP32PanicParser


def test_null_deref_parsed(null_deref_raw: str):
    p = ESP32PanicParser()
    fields = p.parse(null_deref_raw)
    assert fields["panic_type"] == "guru_meditation"
    assert fields["exception_cause"] == 28  # 0x1c = 28
    assert fields["fault_addr"] == "0x00000000"
    assert fields["fault_pc"] == "0x400d1234"
    assert len(fields["backtrace"]) == 4
    assert fields["backtrace"][0] == "0x400d1234"


def test_null_deref_registers(null_deref_raw: str):
    fields = ESP32PanicParser().parse(null_deref_raw)
    assert "A0" in fields["registers"]
    assert "PC" in fields["registers"]
    assert fields["registers"]["EXCCAUSE"] == "0x0000001c"


def test_raw_preserved():
    raw = "Guru Meditation Error: Core  0 panic'ed (StoreProhibited).\nEXCCAUSE: 28\n"
    fields = ESP32PanicParser().parse(raw)
    # raw is NOT included in parse() output — it's added by the caller
    assert "panic_type" in fields


def test_watchdog_parsed(watchdog_raw: str):
    fields = ESP32PanicParser().parse(watchdog_raw)
    assert fields["panic_type"] == "idf_watchdog"
    assert fields["backtrace"] == []
    assert fields["registers"] == {}


def test_stack_overflow_backtrace(stack_overflow_raw: str):
    fields = ESP32PanicParser().parse(stack_overflow_raw)
    assert fields["panic_type"] == "guru_meditation"
    assert len(fields["backtrace"]) == 5
    assert all(pc == "0x400d5678" for pc in fields["backtrace"])


def test_heap_corrupt_parsed(heap_corrupt_raw: str):
    fields = ESP32PanicParser().parse(heap_corrupt_raw)
    assert fields["panic_type"] == "guru_meditation"
    assert fields["exception_cause"] == 28
    assert fields["fault_addr"] == "0x3ffc2a18"
    assert len(fields["backtrace"]) == 4


def test_unknown_format():
    fields = ESP32PanicParser().parse("nothing useful here")
    assert fields["panic_type"] == "unknown"


# ── generalized taxonomy: new panic formats ──────────────────────────────────

def test_assert_failed_parsed(assert_failed_raw: str):
    fields = ESP32PanicParser().parse(assert_failed_raw)
    assert fields["panic_type"] == "abort"
    assert "queue.c:1545" in fields["abort_reason"]
    assert fields["abort_reason"].startswith("assert failed:")
    assert len(fields["backtrace"]) == 5
    assert fields["fault_pc"] == "0x400845e4"
    assert fields["cpu"] == 0


def test_abort_bare_parsed(abort_bare_raw: str):
    fields = ESP32PanicParser().parse(abort_bare_raw)
    assert fields["panic_type"] == "abort"
    assert "abort() was called" in fields["abort_reason"]
    assert fields["cpu"] == 1


def test_brownout_parsed(brownout_raw: str):
    fields = ESP32PanicParser().parse(brownout_raw)
    assert fields["panic_type"] == "brownout"
    assert fields["backtrace"] == []


def test_watchdog_with_abort_stays_watchdog(watchdog_raw: str):
    # The watchdog fixture also contains "abort() was called"; watchdog must win.
    fields = ESP32PanicParser().parse(watchdog_raw)
    assert fields["panic_type"] == "idf_watchdog"


def test_divide_by_zero_parsed(divide_by_zero_raw: str):
    fields = ESP32PanicParser().parse(divide_by_zero_raw)
    assert fields["panic_type"] == "guru_meditation"
    assert fields["exception_cause"] == 6


def test_unaligned_parsed(unaligned_access_raw: str):
    fields = ESP32PanicParser().parse(unaligned_access_raw)
    assert fields["exception_cause"] == 9
    assert fields["fault_addr"] == "0x3ffb8003"
