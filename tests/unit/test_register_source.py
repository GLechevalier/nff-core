"""register_source: record a held-firmware revision WITHOUT building or deploying it.

The instant-crash demo seeds the buggy source this way so get_firmware_source returns a real
bug for the agent to fix, but the device never adopts a buggy image (no slow L2 compile). These
tests pin that it (a) inserts a mock_firmware_revisions row with the right fault/build_id, and
(b) does NOT deploy (no artifact upload / deployment creation).
"""

from __future__ import annotations

import pytest

from nff_ota.source.ops import register_source

BUGGY = """#include "nff.h"
Reading* read_sensor();
void loop() {
  Reading* r = read_sensor();
  publish(r->celsius);   // null deref, no guard
}
"""

CLEAN = """#include "nff.h"
void loop() {
  int x = 1 + 1;
  publish(x);
}
"""


def _device_row(mock, project_id="proj-1"):
    """Point the shared table mock's next .execute() at a devices row with a project."""
    from unittest.mock import MagicMock
    mock.table.return_value.execute.return_value = MagicMock(
        data=[{"id": "mock-x", "project_id": project_id}], error=None
    )


@pytest.mark.asyncio
async def test_register_source_records_revision_with_fault(mock_supabase):
    _device_row(mock_supabase)
    result = await register_source("mock-x", "5.1.0", BUGGY)

    assert "error" not in result
    assert result["version"] == "5.1.0"
    assert result["fault_remaining"] is True          # detect_fault found the null deref
    assert len(result["build_id"]) == 16              # sha256 prefix

    # A revision was inserted (author='seed') — the row get_firmware_source reads back.
    insert = mock_supabase.table.return_value.insert
    insert.assert_called_once()
    row = insert.call_args.args[0]
    assert row["device_id"] == "mock-x"
    assert row["version"] == "5.1.0"
    assert row["source"] == BUGGY
    assert row["author"] == "seed"
    assert row["fault"] == {"scenario": "null_deref"}
    assert row["build_id"] == result["build_id"]


@pytest.mark.asyncio
async def test_register_source_clean_source_has_no_fault(mock_supabase):
    _device_row(mock_supabase)
    result = await register_source("mock-x", "6.0.0", CLEAN)
    assert result["fault_remaining"] is False
    row = mock_supabase.table.return_value.insert.call_args.args[0]
    assert row["fault"] is None


@pytest.mark.asyncio
async def test_register_source_does_not_deploy(mock_supabase, monkeypatch):
    """It must NOT build/upload/deploy — the whole point is skipping the slow L2 compile."""
    import nff_ota.source.ops as ops

    called = {"upload": False, "deploy": False}
    async def _fail_upload(*a, **k):  # pragma: no cover - asserted not called
        called["upload"] = True
        raise AssertionError("register_source must not upload an artifact")
    async def _fail_deploy(*a, **k):  # pragma: no cover
        called["deploy"] = True
        raise AssertionError("register_source must not create a deployment")

    monkeypatch.setattr(ops, "upload_artifact", _fail_upload)
    monkeypatch.setattr(ops, "create_deployment", _fail_deploy)

    _device_row(mock_supabase)
    result = await register_source("mock-x", "5.1.0", BUGGY)
    assert "error" not in result
    assert called == {"upload": False, "deploy": False}


@pytest.mark.asyncio
async def test_register_source_unknown_device(mock_supabase):
    from unittest.mock import MagicMock
    mock_supabase.table.return_value.execute.return_value = MagicMock(data=[], error=None)
    result = await register_source("mock-missing", "5.1.0", BUGGY)
    assert "not found" in result.get("error", "")
