"""End-to-end tests for the nff-diagnosis MCP server.

Unlike tests/test_mcp_tools.py (which calls the tool functions directly), these
boot the *real* FastMCP streamable-HTTP app on a free port with uvicorn and drive
it through `mcp.ClientSession` — exercising the full MCP protocol: transport,
tools/list discovery, JSON-Schema argument validation, and result serialization.
This mirrors the proven pattern in the sibling nff project (nff/tests/test_mcp.py).

Hermetic by default: the Anthropic client is mocked and the store + SQLite DB are
redirected to a tmp dir, so the suite needs no API key and touches no real device.
A separate @pytest.mark.elf layer (deselected by default) proves real
xtensa-esp32-elf-addr2line symbolication when a real ELF + toolchain are present.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP

from nff_diagnosis.capture.parser import ESP32PanicParser
from nff_diagnosis.mcp import config, deps
from nff_diagnosis.mcp.tools.diagnosis import register as register_diagnosis
from nff_diagnosis.mcp.tools.elf import register as register_elf
from nff_diagnosis.mcp.tools.view import register as register_view
from nff_diagnosis.schemas import CrashRecord

ALL_TOOL_NAMES = {
    "diagnose_serial",
    "diagnose_crash",
    "ingest_crash",
    "resolve_backtrace",
    "upload_elf",
    "show_elf",
    "list_crashes",
    "get_crash",
    "get_diagnosis",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _mock_anthropic_factory(text: str = "Root cause narrative."):
    """A drop-in for anthropic.Anthropic that returns a canned narrative."""
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    client = MagicMock()
    client.messages.create.return_value = msg
    return MagicMock(return_value=client)


def _clear_dep_caches() -> None:
    deps.get_store.cache_clear()
    deps.get_storage.cache_clear()
    deps.get_engine.cache_clear()
    deps.get_resolver.cache_clear()


def _crash_arg(raw: str, build_id: str, *, device_id: str | None = None) -> dict:
    """Parse a raw panic fixture into a JSON-ready CrashRecord dict for a tool arg."""
    fields = ESP32PanicParser().parse(raw)
    fields["build_id"] = build_id
    fields["raw"] = raw
    if device_id:
        fields["device_id"] = device_id
    return CrashRecord(**fields).model_dump(mode="json")


@asynccontextmanager
async def _session(url: str):
    """Open an initialized MCP client session against the live server."""
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _payload(result):
    """Decode a single object/scalar tool result (one JSON text block)."""
    return json.loads(result.content[0].text)


def _payload_list(result):
    """Decode a list-returning tool result.

    FastMCP emits one text block per list item (and zero blocks for an empty list),
    rather than a single JSON-array block — so collect every block.
    """
    return [json.loads(c.text) for c in result.content]


# ---------------------------------------------------------------------------
# Fixture: live HTTP MCP server on a free port (in-process, hermetic)
# ---------------------------------------------------------------------------

@pytest.fixture()
async def diag_mcp_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Start the real FastMCP streamable-HTTP app; yield its /mcp URL.

    Redirects the ELF store + SQLite DB to tmp_path and mocks the Anthropic client
    so tool calls are deterministic, free, and isolated from any real data.
    """
    monkeypatch.setattr(config.settings, "store_root", tmp_path / "store")
    monkeypatch.setattr(config.settings, "db_path", tmp_path / "crashes.db")
    _clear_dep_caches()

    # Build a fresh FastMCP per test. The production server.mcp singleton's
    # StreamableHTTPSessionManager can only be run() once, so reusing it would hang
    # every test after the first. Registering the same tool callables onto a new
    # instance gives each test an isolated server with identical behaviour.
    mcp = FastMCP(name="nff-diagnosis-e2e")
    register_diagnosis(mcp)
    register_elf(mcp)
    register_view(mcp)

    port = _free_port()
    with patch(
        "nff_diagnosis.engine.diagnosis.anthropic.Anthropic", _mock_anthropic_factory()
    ):
        app = mcp.streamable_http_app()
        cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        uv = uvicorn.Server(cfg)
        task = asyncio.create_task(uv.serve())
        try:
            while not uv.started:
                await asyncio.sleep(0.05)
            yield f"http://127.0.0.1:{port}/mcp"
        finally:
            uv.should_exit = True
            await asyncio.wait_for(task, timeout=5.0)
    _clear_dep_caches()


# ===========================================================================
# Hermetic E2E — all 9 tools over the real MCP protocol
# ===========================================================================

async def test_e2e_lists_all_nine_tools(diag_mcp_url: str):
    async with _session(diag_mcp_url) as session:
        result = await session.list_tools()
    assert {t.name for t in result.tools} == ALL_TOOL_NAMES


async def test_e2e_upload_and_show_elf(diag_mcp_url: str, tmp_path: Path):
    elf_file = tmp_path / "firmware.elf"
    elf_file.write_bytes(b"\x7fELF fake bytes")

    async with _session(diag_mcp_url) as session:
        up = await session.call_tool(
            "upload_elf",
            {
                "params": {
                    "elf_path": str(elf_file),
                    "customer": "acme",
                    "project": "sensor-node",
                    "firmware_version": "1.0.0",
                }
            },
        )
        record = _payload(up)
        assert record["customer"] == "acme"
        build_id = record["build_id"]

        shown = _payload(await session.call_tool("show_elf", {"build_id": build_id}))
    assert shown["build_id"] == build_id


async def test_e2e_show_elf_unknown_returns_error(diag_mcp_url: str):
    async with _session(diag_mcp_url) as session:
        result = _payload(await session.call_tool("show_elf", {"build_id": "nope"}))
    assert "error" in result


async def test_e2e_diagnose_serial_null_deref(diag_mcp_url: str, null_deref_raw: str):
    async with _session(diag_mcp_url) as session:
        result = _payload(
            await session.call_tool(
                "diagnose_serial",
                {"params": {"raw": null_deref_raw, "build_id": "fakebuildid"}},
            )
        )
    assert result["crash_class"] == "null_deref"
    assert 0.0 <= result["confidence"] <= 1.0


async def test_e2e_ingest_then_view_roundtrip(diag_mcp_url: str, watchdog_raw: str):
    crash = _crash_arg(watchdog_raw, build_id="fakebuildid")

    async with _session(diag_mcp_url) as session:
        diag = _payload(await session.call_tool("ingest_crash", {"crash": crash}))
        assert diag["crash_class"] == "watchdog"

        summaries = _payload_list(await session.call_tool("list_crashes", {}))
        assert any(s["crash_id"] == crash["crash_id"] for s in summaries)

        got = _payload(
            await session.call_tool("get_crash", {"crash_id": crash["crash_id"]})
        )
        assert got["crash_id"] == crash["crash_id"]

        stored_diag = _payload(
            await session.call_tool("get_diagnosis", {"crash_id": crash["crash_id"]})
        )
    assert stored_diag["crash_id"] == crash["crash_id"]


async def test_e2e_diagnose_crash_not_persisted(diag_mcp_url: str, null_deref_raw: str):
    crash = _crash_arg(null_deref_raw, build_id="fakebuildid")

    async with _session(diag_mcp_url) as session:
        diag = _payload(await session.call_tool("diagnose_crash", {"crash": crash}))
        assert diag["crash_class"] == "null_deref"
        # diagnose_crash explicitly does NOT persist — it must not appear in the view.
        summaries = _payload_list(await session.call_tool("list_crashes", {}))
    assert all(s["crash_id"] != crash["crash_id"] for s in summaries)


async def test_e2e_resolve_backtrace_no_elf_degrades(diag_mcp_url: str, null_deref_raw: str):
    crash = _crash_arg(null_deref_raw, build_id="missing")

    async with _session(diag_mcp_url) as session:
        result = _payload(await session.call_tool("resolve_backtrace", {"crash": crash}))
    # No ELF for this build_id → unresolved frames, empty location, no error.
    assert "error" not in result
    assert result["location"] == {}


async def test_e2e_multiple_sequential_calls(diag_mcp_url: str, watchdog_raw: str):
    crash = _crash_arg(watchdog_raw, build_id="fakebuildid")
    async with _session(diag_mcp_url) as session:
        first = _payload_list(await session.call_tool("list_crashes", {}))
        assert first == []
        await session.call_tool("ingest_crash", {"crash": crash})
        second = _payload_list(await session.call_tool("list_crashes", {}))
    assert len(second) == 1


# ===========================================================================
# Opt-in: real ESP32 symbolication (run with `-m elf`)
# ===========================================================================

def _real_elf_skip_reason() -> str | None:
    if shutil.which("xtensa-esp32-elf-addr2line") is None:
        return "xtensa-esp32-elf-addr2line not on PATH"
    elf = os.environ.get("NFF_DIAG_TEST_ELF")
    crash = os.environ.get("NFF_DIAG_TEST_CRASH")
    if not elf or not Path(elf).is_file():
        return "set NFF_DIAG_TEST_ELF to a real ESP32 .elf"
    if not crash or not Path(crash).is_file():
        return "set NFF_DIAG_TEST_CRASH to a panic dump matching that ELF"
    return None


@pytest.mark.elf
async def test_e2e_real_symbolication(diag_mcp_url: str):
    reason = _real_elf_skip_reason()
    if reason:
        pytest.skip(reason)

    elf_path = os.environ["NFF_DIAG_TEST_ELF"]
    raw = Path(os.environ["NFF_DIAG_TEST_CRASH"]).read_text(encoding="utf-8")

    async with _session(diag_mcp_url) as session:
        record = _payload(
            await session.call_tool(
                "upload_elf",
                {
                    "params": {
                        "elf_path": elf_path,
                        "customer": "e2e",
                        "project": "real-fw",
                        "firmware_version": "test",
                    }
                },
            )
        )
        build_id = record["build_id"]

        crash = _crash_arg(raw, build_id=build_id)
        resolved = _payload(
            await session.call_tool("resolve_backtrace", {"crash": crash})
        )

    assert "error" not in resolved
    # With a matching real ELF + toolchain, at least one frame must resolve.
    assert resolved["resolved_backtrace"], "expected at least one resolved frame"
    assert resolved["location"].get("function") or resolved["location"].get("file"), (
        "expected a real function/file for the primary frame"
    )
