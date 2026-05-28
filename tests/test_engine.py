from unittest.mock import MagicMock, patch

from nff_diagnosis.capture.parser import ESP32PanicParser
from nff_diagnosis.engine.diagnosis import DiagnosisEngine
from nff_diagnosis.schemas import CrashRecord
from nff_diagnosis.schemas.diagnosis_output import DiagnosisOutput, ResolvedFrame


def _make_crash(raw: str) -> CrashRecord:
    fields = ESP32PanicParser().parse(raw)
    fields["build_id"] = "test"
    fields["raw"] = raw
    return CrashRecord(**fields)


def _mock_anthropic(text: str):
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    client = MagicMock()
    client.messages.create.return_value = msg
    return MagicMock(return_value=client)


def test_engine_null_deref(null_deref_raw: str):
    crash = _make_crash(null_deref_raw)
    resolved_bt = [ResolvedFrame(pc=pc, function="fn", file="f.c", line=1) for pc in crash.backtrace]
    location = {"function": "fn", "file": "f.c", "line": 1}

    with patch("nff_diagnosis.engine.diagnosis.anthropic.Anthropic", _mock_anthropic("Null pointer fault.")):
        engine = DiagnosisEngine(api_key="fake")
        diag = engine.diagnose(crash, resolved_bt, location)

    assert isinstance(diag, DiagnosisOutput)
    assert diag.crash_class == "null_deref"
    assert diag.confidence >= 0.90
    assert "Null pointer" in diag.root_cause
    assert diag.engine_version == "nff-diagnosis/0.1.0"
    DiagnosisOutput.model_validate(diag.model_dump())  # round-trip


def test_engine_watchdog(watchdog_raw: str):
    crash = _make_crash(watchdog_raw)
    with patch("nff_diagnosis.engine.diagnosis.anthropic.Anthropic", _mock_anthropic("WDT triggered.")):
        engine = DiagnosisEngine(api_key="fake")
        diag = engine.diagnose(crash, [], {})
    assert diag.crash_class == "watchdog"
    assert diag.confidence >= 0.95


def test_engine_stack_overflow(stack_overflow_raw: str):
    crash = _make_crash(stack_overflow_raw)
    with patch("nff_diagnosis.engine.diagnosis.anthropic.Anthropic", _mock_anthropic("Stack overflow.")):
        engine = DiagnosisEngine(api_key="fake")
        diag = engine.diagnose(crash, [], {})
    assert diag.crash_class == "stack_overflow"


def test_engine_confidence_always_set(null_deref_raw: str):
    crash = _make_crash(null_deref_raw)
    with patch("nff_diagnosis.engine.diagnosis.anthropic.Anthropic", _mock_anthropic("cause")):
        diag = DiagnosisEngine(api_key="fake").diagnose(crash, [], {})
    assert 0.0 <= diag.confidence <= 1.0
