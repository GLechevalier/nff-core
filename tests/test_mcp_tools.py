"""Unit tests for the MCP tool layer.

The tools call the same in-process engine/store/storage as the REST server, so the
checks mirror tests/test_server.py: monkeypatch the Anthropic client and point the
deps singletons at a tmp store + SQLite DB. We call the tool functions directly
(the FastMCP `add_tool` wiring is a thin pass-through over these same callables).
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nff_diagnosis.engine.diagnosis import DiagnosisEngine
from nff_diagnosis.resolution.addr2line import Addr2LineResolver
from nff_diagnosis.schemas import CrashRecord
from nff_diagnosis.schemas.diagnosis_output import DiagnosisOutput
from nff_diagnosis.storage.sqlite import SQLiteStorage
from nff_diagnosis.store.local import LocalELFStore

from nff_diagnosis.mcp.tools.diagnosis import ops as diag_ops
from nff_diagnosis.mcp.tools.elf import ops as elf_ops
from nff_diagnosis.mcp.tools.view import ops as view_ops


def _mock_anthropic_factory(text: str = "Root cause narrative."):
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    client = MagicMock()
    client.messages.create.return_value = msg
    return MagicMock(return_value=client)


@pytest.fixture()
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point every tool module's deps at tmp instances with a mocked LLM."""
    store = LocalELFStore(tmp_path / "store")
    storage = SQLiteStorage(tmp_path / "test.db")
    resolver = Addr2LineResolver()
    with patch("nff_diagnosis.engine.diagnosis.anthropic.Anthropic", _mock_anthropic_factory()):
        engine = DiagnosisEngine(api_key="fake")
        for mod in (diag_ops, elf_ops, view_ops):
            if hasattr(mod, "get_store"):
                monkeypatch.setattr(mod, "get_store", lambda: store, raising=False)
            if hasattr(mod, "get_storage"):
                monkeypatch.setattr(mod, "get_storage", lambda: storage, raising=False)
            if hasattr(mod, "get_engine"):
                monkeypatch.setattr(mod, "get_engine", lambda: engine, raising=False)
            if hasattr(mod, "get_resolver"):
                monkeypatch.setattr(mod, "get_resolver", lambda: resolver, raising=False)
        yield {"store": store, "storage": storage}


async def test_diagnose_serial_null_deref(wired, null_deref_raw: str):
    diag = await diag_ops.diagnose_serial(
        diag_ops.DiagnoseSerialParams(raw=null_deref_raw, build_id="fakebuildid")
    )
    assert isinstance(diag, DiagnosisOutput)
    assert diag.crash_class == "null_deref"
    assert 0.0 <= diag.confidence <= 1.0
    # diagnose_serial persists via run_pipeline → visible to the view tools.
    assert wired["storage"].get_diagnosis(diag.crash_id) is not None


async def test_ingest_then_view_roundtrip(wired, watchdog_raw: str):
    from nff_diagnosis.capture.parser import ESP32PanicParser

    fields = ESP32PanicParser().parse(watchdog_raw)
    fields["build_id"] = "fakebuildid"
    fields["raw"] = watchdog_raw
    crash = CrashRecord(**fields)

    diag = await diag_ops.ingest_crash(crash)
    assert isinstance(diag, DiagnosisOutput)
    assert diag.crash_class == "watchdog"

    summaries = await view_ops.list_crashes()
    assert any(s.crash_id == crash.crash_id for s in summaries)
    got = await view_ops.get_crash(crash.crash_id)
    assert isinstance(got, CrashRecord)
    assert got.crash_id == crash.crash_id


async def test_resolve_backtrace_no_elf_degrades(wired, null_deref_raw: str):
    from nff_diagnosis.capture.parser import ESP32PanicParser

    fields = ESP32PanicParser().parse(null_deref_raw)
    fields["build_id"] = "missing"
    fields["raw"] = null_deref_raw
    crash = CrashRecord(**fields)

    result = await diag_ops.resolve_backtrace(crash)
    # No ELF stored for this build_id → resolves to unresolved frames, no exception.
    assert isinstance(result, diag_ops.ResolveResult)
    assert result.location == {}


async def test_upload_and_show_elf(wired, tmp_path: Path):
    elf_file = tmp_path / "firmware.elf"
    elf_file.write_bytes(b"\x7fELF fake bytes")
    record = await elf_ops.upload_elf(
        elf_ops.UploadElfParams(
            elf_path=str(elf_file),
            customer="acme",
            project="sensor-node",
            firmware_version="1.0.0",
        )
    )
    assert record.customer == "acme"
    shown = await elf_ops.show_elf(record.build_id)
    assert shown.build_id == record.build_id


async def test_show_elf_unknown_returns_error(wired):
    result = await elf_ops.show_elf("does-not-exist")
    assert isinstance(result, dict) and "error" in result
