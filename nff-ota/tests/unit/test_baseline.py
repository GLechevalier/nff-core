"""Tests for the universal L2 baseline source of record (source/baseline.py).

The behaviour that matters: an L2 mock with no revision row still has a KNOWN firmware source
(the baseline image every L2 boots), so it must never be reported as "no firmware source on
record" — that null is what made a prod incident unrecoverable. These cover the happy path, the
device kinds that are correctly skipped, and the two ways it must refuse rather than record
something false.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from nff_ota import github
from nff_ota.source import baseline, ops
from nff_ota.source.compile import build_id_for

L2_DEV = "mock-b2b2b2b2-735075"
REAL_SOURCE = '#include "nff.h"\nvoid app_main(void) { nff_init(&g_cfg); }\n'
# The placeholder nff-github serves when its workspace mount is missing (babynff_mock._FILES).
STUB_SOURCE = (
    "// babynff demo firmware — native ESP-IDF / FreeRTOS entrypoint.\n"
    "// The authoritative source is nff-mock/qemu/l2/example-espidf/clean_l2.c (synced from\n"
    "// nff-dashboard/src/data/babynffFirmware.ts); this stub is served only when that file\n"
    "// is unreachable from this process.\n"
)


def _resp(data):
    return MagicMock(data=data)


def _device(device_type="nff-qemu-esp32", project_id="p1"):
    return _resp([{"project_id": project_id, "device_type": device_type}])


async def test_records_baseline_for_l2_without_revision(mock_supabase, monkeypatch):
    monkeypatch.setattr(github, "read_repo_file", AsyncMock(return_value=REAL_SOURCE))
    # has_revision → none; devices → an L2; insert → ok.
    mock_supabase.table.return_value.execute = AsyncMock(
        side_effect=[_resp([]), _device(), _resp([{}])]
    )

    assert await baseline.ensure_baseline(L2_DEV) is True

    row = mock_supabase.table.return_value.insert.call_args[0][0]
    assert row["device_id"] == L2_DEV
    assert row["author"] == "baseline"
    assert row["source"] == REAL_SOURCE
    assert row["fault"] is None          # the CLEAN twin by definition
    assert row["parent_version"] is None  # it is the root of the chain
    # The build_id contract seed_baseline.sh stamps into the image: sha256(source)[:16].
    assert row["build_id"] == build_id_for(REAL_SOURCE)


async def test_baseline_version_is_a_bare_semver(mock_supabase, monkeypatch):
    """The floor is only enforceable if ops._SEMVER can parse it.

    A suffixed "0.0.0-l2" (which is what the device reports on the wire) does NOT match, and
    would silently disable submit_firmware_fix's anti-downgrade check.
    """
    assert ops._SEMVER.match(baseline.BASELINE_VERSION)
    assert not ops._SEMVER.match("0.0.0-l2")


async def test_refuses_to_record_the_placeholder_stub(mock_supabase, monkeypatch):
    """Recording the stub would be worse than recording nothing — an agent would 'fix' a comment."""
    monkeypatch.setattr(github, "read_repo_file", AsyncMock(return_value=STUB_SOURCE))
    mock_supabase.table.return_value.execute = AsyncMock(side_effect=[_resp([]), _device()])

    assert await baseline.ensure_baseline(L2_DEV) is False
    mock_supabase.table.return_value.insert.assert_not_called()


async def test_noop_when_a_revision_already_exists(mock_supabase, monkeypatch):
    read = AsyncMock(return_value=REAL_SOURCE)
    monkeypatch.setattr(github, "read_repo_file", read)
    mock_supabase.table.return_value.execute = AsyncMock(side_effect=[_resp([{"id": "r1"}])])

    assert await baseline.ensure_baseline(L2_DEV) is True
    read.assert_not_awaited()  # no needless HTTP hop to nff-github
    mock_supabase.table.return_value.insert.assert_not_called()


@pytest.mark.parametrize("device_type", ["nff-mock-esp32", "esp32", ""])
async def test_skips_non_l2_devices(mock_supabase, monkeypatch, device_type):
    """Only L2 has a known universal baseline; an L1 sketch is invented per-demo."""
    monkeypatch.setattr(github, "read_repo_file", AsyncMock(return_value=REAL_SOURCE))
    mock_supabase.table.return_value.execute = AsyncMock(
        side_effect=[_resp([]), _device(device_type=device_type)]
    )

    assert await baseline.ensure_baseline("mock-l1-1") is False
    mock_supabase.table.return_value.insert.assert_not_called()


async def test_unknown_device_is_not_an_error(mock_supabase, monkeypatch):
    monkeypatch.setattr(github, "read_repo_file", AsyncMock(return_value=REAL_SOURCE))
    mock_supabase.table.return_value.execute = AsyncMock(side_effect=[_resp([]), _resp([])])

    assert await baseline.ensure_baseline("ghost") is False


async def test_github_failure_degrades_instead_of_raising(mock_supabase, monkeypatch):
    """A device with no baseline is exactly the old behaviour — never break the caller."""
    monkeypatch.setattr(
        github, "read_repo_file", AsyncMock(side_effect=github.GithubError("unreachable"))
    )
    mock_supabase.table.return_value.execute = AsyncMock(side_effect=[_resp([]), _device()])

    assert await baseline.ensure_baseline(L2_DEV) is False
    mock_supabase.table.return_value.insert.assert_not_called()


async def test_get_firmware_source_materialises_then_returns_it(mock_supabase, monkeypatch):
    """The end-to-end point: a crashing L2 is never a dead end for the agent."""
    rev = {
        "version": baseline.BASELINE_VERSION, "source": REAL_SOURCE,
        "build_id": build_id_for(REAL_SOURCE), "fault": None, "author": "baseline",
    }
    # latest → none; then ensure_baseline succeeds; then latest → the baseline row.
    monkeypatch.setattr(baseline, "ensure_baseline", AsyncMock(return_value=True))
    mock_supabase.table.return_value.execute = AsyncMock(
        side_effect=[_resp([]), _resp([rev])]
    )

    res = await ops.get_firmware_source(L2_DEV, include_source=True)
    assert res.error is None
    assert res.source.source == REAL_SOURCE
    assert res.source.version == baseline.BASELINE_VERSION
    assert res.source.author == "baseline"


async def test_get_firmware_source_still_errors_when_no_baseline(mock_supabase, monkeypatch):
    monkeypatch.setattr(baseline, "ensure_baseline", AsyncMock(return_value=False))
    mock_supabase.table.return_value.execute = AsyncMock(side_effect=[_resp([])])

    res = await ops.get_firmware_source("mock-l1-1")
    assert "no firmware source on record" in res.error
