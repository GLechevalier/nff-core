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
        exception_cause=29,
        fault_addr="0x400d0000",
        raw="test",
    )
    result = RuleClassifier().classify(crash)
    assert result.crash_class == "null_deref"
    assert result.confidence < 0.80
    assert len(result.candidates) >= 1


# ── generalized taxonomy: the Recommended-6 new classes ──────────────────────

def test_divide_by_zero_classified(divide_by_zero_raw: str):
    result = RuleClassifier().classify(_make_crash(divide_by_zero_raw))
    assert result.crash_class == "divide_by_zero"
    assert result.confidence >= 0.95


def test_unaligned_access_classified(unaligned_access_raw: str):
    result = RuleClassifier().classify(_make_crash(unaligned_access_raw))
    assert result.crash_class == "unaligned_access"
    assert result.confidence >= 0.90


def test_bad_instruction_fetch_classified(bad_instruction_fetch_raw: str):
    result = RuleClassifier().classify(_make_crash(bad_instruction_fetch_raw))
    assert result.crash_class == "bad_instruction_fetch"
    assert result.confidence >= 0.85


def test_illegal_instruction_no_repeat_classified(illegal_instruction_raw: str):
    # EXCCAUSE=0 WITHOUT a stuck-unwinder signature → illegal_instruction, NOT stack_overflow.
    result = RuleClassifier().classify(_make_crash(illegal_instruction_raw))
    assert result.crash_class == "illegal_instruction"
    assert any(c.crash_class == "bad_instruction_fetch" for c in result.candidates)


def test_assert_failed_classified(assert_failed_raw: str):
    result = RuleClassifier().classify(_make_crash(assert_failed_raw))
    assert result.crash_class == "assert_failed"
    assert result.confidence >= 0.90


def test_abort_bare_classified(abort_bare_raw: str):
    # A bare abort() with no assert message still classifies as assert_failed (not watchdog).
    result = RuleClassifier().classify(_make_crash(abort_bare_raw))
    assert result.crash_class == "assert_failed"


def test_brownout_classified(brownout_raw: str):
    result = RuleClassifier().classify(_make_crash(brownout_raw))
    assert result.crash_class == "brownout"
    assert result.confidence >= 0.90


def test_watchdog_with_abort_is_still_watchdog(watchdog_raw: str):
    # A real task-WDT dump ends in abort(); the watchdog signature must win over abort.
    result = RuleClassifier().classify(_make_crash(watchdog_raw))
    assert result.crash_class == "watchdog"
