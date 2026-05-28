from nff_diagnosis.capture.parser import ESP32PanicParser
from nff_diagnosis.engine.classifier import RuleClassifier
from nff_diagnosis.schemas import CrashRecord


def _make_crash(raw: str, build_id: str = "test") -> CrashRecord:
    fields = ESP32PanicParser().parse(raw)
    fields["build_id"] = build_id
    fields["raw"] = raw
    return CrashRecord(**fields)


def test_watchdog_classified(watchdog_raw: str):
    crash = _make_crash(watchdog_raw)
    result = RuleClassifier().classify(crash)
    assert result.crash_class == "watchdog"
    assert result.confidence >= 0.95


def test_null_deref_exccause28_addr0(null_deref_raw: str):
    crash = _make_crash(null_deref_raw)
    result = RuleClassifier().classify(crash)
    assert result.crash_class == "null_deref"
    assert result.confidence >= 0.95


def test_stack_overflow_repeated_pcs(stack_overflow_raw: str):
    crash = _make_crash(stack_overflow_raw)
    result = RuleClassifier().classify(crash)
    assert result.crash_class == "stack_overflow"
    assert result.confidence >= 0.90


def test_heap_corrupt_has_candidates(heap_corrupt_raw: str):
    crash = _make_crash(heap_corrupt_raw)
    result = RuleClassifier().classify(crash)
    assert result.crash_class == "heap_corruption"
    assert len(result.candidates) >= 1
    assert result.confidence < 0.80


def test_unknown_crash_low_confidence():
    crash = CrashRecord(build_id="x", panic_type="unknown", raw="nothing")
    result = RuleClassifier().classify(crash)
    assert result.crash_class == "unknown"
    assert result.confidence < 0.50


def test_store_prohibited_non_null_has_candidates():
    crash = CrashRecord(
        build_id="x",
        panic_type="guru_meditation",
        exception_cause=28,
        fault_addr="0x400d0000",
        raw="test",
    )
    result = RuleClassifier().classify(crash)
    assert result.crash_class == "null_deref"
    assert result.confidence < 0.80
    assert len(result.candidates) >= 1
