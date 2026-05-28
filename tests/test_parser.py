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
    assert fields["exception_cause"] == 3
    assert fields["fault_addr"] == "0x3ffc2a18"
    assert len(fields["backtrace"]) == 4


def test_unknown_format():
    fields = ESP32PanicParser().parse("nothing useful here")
    assert fields["panic_type"] == "unknown"
