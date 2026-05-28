import json

from nff_diagnosis.schemas import CrashRecord, DiagnosisOutput, ELFRecord
from nff_diagnosis.schemas.diagnosis_output import Candidate, ResolvedFrame


def test_elf_record_defaults():
    r = ELFRecord(
        build_id="abc",
        elf_path="/tmp/fw.elf",
        firmware_version="1.0",
        project="p",
        customer="c",
    )
    assert r.target == "esp32"
    assert r.created_at is not None


def test_crash_record_auto_id():
    c = CrashRecord(build_id="abc", panic_type="guru_meditation", raw="raw text")
    assert c.crash_id
    assert c.raw == "raw text"
    assert c.backtrace == []
    assert c.registers == {}


def test_crash_record_roundtrip():
    c = CrashRecord(build_id="abc", panic_type="guru_meditation", raw="r")
    restored = CrashRecord.model_validate_json(c.model_dump_json())
    assert restored.crash_id == c.crash_id


def test_diagnosis_output_roundtrip():
    d = DiagnosisOutput(
        crash_id="x",
        crash_class="null_deref",
        root_cause="Null pointer in main.",
        location={"function": "main", "file": "main.c", "line": 10},
        resolved_backtrace=[ResolvedFrame(pc="0x400d1234", function="main")],
        confidence=0.96,
        candidates=[],
        engine_version="nff-diagnosis/0.1.0",
    )
    restored = DiagnosisOutput.model_validate_json(d.model_dump_json())
    assert restored.crash_class == "null_deref"
    assert restored.confidence == 0.96
    assert restored.resolved_backtrace[0].function == "main"
