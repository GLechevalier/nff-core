import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nff_diagnosis.capture.parser import ESP32PanicParser
from nff_diagnosis.resolution.addr2line import Addr2LineResolver
from nff_diagnosis.schemas import CrashRecord


def _make_crash(raw_path: Path) -> CrashRecord:
    raw = raw_path.read_text(encoding="utf-8")
    fields = ESP32PanicParser().parse(raw)
    fields["build_id"] = "test"
    fields["raw"] = raw
    return CrashRecord(**fields)


def test_resolve_enriches_backtrace(monkeypatch, null_deref_raw: str, tmp_path: Path):
    fake_elf = tmp_path / "fw.elf"
    fake_elf.write_bytes(b"\x7fELF")

    addr2line_output = (
        "app_main at /src/app_main.c:42\n"
        "poll_sensors at /src/sensors.c:88\n"
        "some_func at /src/util.c:10\n"
        "other_func at /src/util.c:20\n"
    )

    mock_result = MagicMock()
    mock_result.stdout = addr2line_output
    mock_result.returncode = 0
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)

    fields = ESP32PanicParser().parse(null_deref_raw)
    fields["build_id"] = "test"
    fields["raw"] = null_deref_raw
    crash = CrashRecord(**fields)

    bt, loc = Addr2LineResolver().resolve(crash, fake_elf)
    assert len(bt) == len(crash.backtrace)
    assert bt[0].function == "app_main"
    assert bt[0].file == "/src/app_main.c"
    assert bt[0].line == 42
    assert loc["function"] == "app_main"


def test_resolve_no_elf(null_deref_raw: str):
    fields = ESP32PanicParser().parse(null_deref_raw)
    fields["build_id"] = "test"
    fields["raw"] = null_deref_raw
    crash = CrashRecord(**fields)

    bt, loc = Addr2LineResolver().resolve(crash, None)
    assert len(bt) == len(crash.backtrace)
    assert all(f.function is None for f in bt)
    assert loc == {}


def test_resolve_addr2line_not_found(monkeypatch, null_deref_raw: str, tmp_path: Path):
    fake_elf = tmp_path / "fw.elf"
    fake_elf.write_bytes(b"\x7fELF")

    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(side_effect=FileNotFoundError("not found")),
    )

    fields = ESP32PanicParser().parse(null_deref_raw)
    fields["build_id"] = "test"
    fields["raw"] = null_deref_raw
    crash = CrashRecord(**fields)

    bt, loc = Addr2LineResolver().resolve(crash, fake_elf)
    assert all(f.function is None for f in bt)


def test_resolve_question_marks_become_none(monkeypatch, null_deref_raw: str, tmp_path: Path):
    fake_elf = tmp_path / "fw.elf"
    fake_elf.write_bytes(b"\x7fELF")

    mock_result = MagicMock()
    mock_result.stdout = "?? at ??:0\n?? at ??:0\n?? at ??:0\n?? at ??:0\n"
    mock_result.returncode = 0
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)

    fields = ESP32PanicParser().parse(null_deref_raw)
    fields["build_id"] = "test"
    fields["raw"] = null_deref_raw
    crash = CrashRecord(**fields)

    bt, loc = Addr2LineResolver().resolve(crash, fake_elf)
    assert all(f.function is None for f in bt)
    assert loc == {}
