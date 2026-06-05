"""Integration test for start_deployment — delivery via nff-fleet."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import nff_ota.fleet as fleet
import nff_ota.tools.deployments.ops as ops
from nff_ota.tools.deployments.ops import start_deployment


def _dep_row(status: str = "active") -> dict:
    return {
        "id": "dep-1",
        "name": "deploy-v1",
        "target_version": "1.0.1",
        "device_group_id": "grp-1",
        "status": status,
        "max_devices_in_flight": 10,
        "retries": 3,
        "created_at": "2026-01-01T00:00:00+00:00",
        "start_time": None,
    }


def _fw_row() -> dict:
    return {"version": "1.0.1", "checksum_sha256": "abc123", "storage_path": "firmware/1.0.1.bin"}


def _job_row(job_id: str, dev_id: str, status: str = "pending") -> dict:
    return {
        "id": job_id,
        "deployment_id": "dep-1",
        "device_id": dev_id,
        "from_version": "1.0.0",
        "target_version": "1.0.1",
        "status": status,
        "progress": 0,
        "attempts": 0,
        "started_at": "2026-01-01T00:00:00+00:00",
        "last_updated": "2026-01-01T00:00:00+00:00",
    }


@pytest.fixture
def _patch_signed_url(monkeypatch):
    monkeypatch.setattr(ops, "create_signed_url", AsyncMock(return_value="https://signed/fw.bin"))


def _table_se(responses: dict[str, list[list]]):
    counters: dict[str, int] = {}

    def table_se(name):
        t = MagicMock()
        for chain in ("select", "insert", "update", "eq", "in_", "order"):
            getattr(t, chain).return_value = t

        async def execute():
            n = counters.get(name, 0)
            counters[name] = n + 1
            seq = responses.get(name, [[]])
            return MagicMock(data=seq[n] if n < len(seq) else [], error=None)

        t.execute = execute
        return t

    return table_se


async def test_start_deployment_delivers_ota_per_device(mock_supabase, monkeypatch, _patch_signed_url):
    send_mock = AsyncMock(return_value={"status": "ota_started"})
    monkeypatch.setattr(fleet, "send_command", send_mock)

    responses: dict[str, list[list]] = {
        "deployments": [
            [_dep_row("active")],                     # initial load
            [_dep_row("active")],                     # recompute: load
            [_dep_row("active")],                     # final return
        ],
        "firmware_versions": [[_fw_row()]],
        "ota_jobs": [
            [_job_row("job-1", "device-1"), _job_row("job-2", "device-2")],  # pending select
            [],                                       # update job-1 -> downloading
            [],                                       # update job-2 -> downloading
            [_job_row("job-1", "device-1", "downloading"),
             _job_row("job-2", "device-2", "downloading")],                  # recompute select
        ],
        "devices": [[
            {"id": "device-1", "identity_attributes": {"mqtt_id": "device-1"}},
            {"id": "device-2", "identity_attributes": {"mqtt_id": "device-2"}},
        ]],
    }
    mock_supabase.table.side_effect = _table_se(responses)

    result = await start_deployment("dep-1")

    assert result.error is None
    assert result.delivered == 2
    assert result.failed == 0
    assert result.skipped == 0
    assert send_mock.await_count == 2

    # Every delivery carries a correctly built OTA payload.
    for call in send_mock.await_args_list:
        mqtt_id, payload = call.args[0], call.args[1]
        assert mqtt_id in ("device-1", "device-2")
        assert payload == {
            "action": "ota",
            "version": "1.0.1",
            "sha256": "abc123",
            "url": "https://signed/fw.bin",
        }


async def test_start_deployment_skips_device_without_mqtt_id(mock_supabase, monkeypatch, _patch_signed_url):
    send_mock = AsyncMock(return_value={"status": "ota_started"})
    monkeypatch.setattr(fleet, "send_command", send_mock)

    responses: dict[str, list[list]] = {
        "deployments": [[_dep_row("active")], [_dep_row("active")], [_dep_row("active")]],
        "firmware_versions": [[_fw_row()]],
        "ota_jobs": [
            [_job_row("job-1", "device-1")],
            [_job_row("job-1", "device-1", "downloading")],  # recompute select (no update happened)
        ],
        "devices": [[{"id": "device-1", "identity_attributes": {"mac": "aa:bb"}}]],
    }
    mock_supabase.table.side_effect = _table_se(responses)

    result = await start_deployment("dep-1")

    assert result.error is None
    assert result.skipped == 1
    assert result.delivered == 0
    assert send_mock.await_count == 0
