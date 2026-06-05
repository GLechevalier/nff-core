"""Integration tests for device tools using a mocked Supabase client."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nff_ota.models.device import ListDevicesParams, PreauthorizeDeviceParams, RegisterDeviceParams
from nff_ota.tools.devices.ops import (
    accept_device,
    decommission_device,
    list_devices,
    preauthorize_device,
    register_device,
    reject_device,
)


def _device_row(
    device_id: str = "dev-1",
    ota_status: str = "pending",
    identity: dict | None = None,
) -> dict:
    return {
        "id": device_id,
        "name": device_id,
        "status": "offline",            # fleet connectivity status
        "firmware_version": "unknown",
        "identity_attributes": identity or {"mac": "aa:bb"},
        "public_key": None,
        "ota_status": ota_status,       # nff-ota enrollment status
        "current_firmware_version": None,
        "tags": [],
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def _seq_tables(responses: list[list[dict]]) -> MagicMock:
    """side_effect for client.table(): each successive .execute() (awaited) returns the next response."""
    call_n = [0]

    def side_effect(name):
        t = MagicMock()
        for m in ("select", "insert", "update", "delete", "eq", "in_", "neq", "order", "limit"):
            getattr(t, m).return_value = t

        async def _execute():
            idx = call_n[0]
            call_n[0] += 1
            data = responses[idx] if idx < len(responses) else []
            return MagicMock(data=data, error=None)

        t.execute = _execute
        return t

    return side_effect


class TestRegisterDevice:
    async def test_creates_pending_device(self, mock_supabase):
        row = _device_row()
        # 1) lookup existing by id -> none, 2) insert -> row
        mock_supabase.table.side_effect = _seq_tables([[], [row]])

        result = await register_device(
            RegisterDeviceParams(device_id="dev-1", identity_attributes={"mac": "aa:bb"})
        )
        assert result.error is None
        assert result.device is not None
        assert result.device.id == "dev-1"
        assert result.device.status == "pending"

    async def test_existing_preauthorized_auto_accepts(self, mock_supabase):
        identity = {"mac": "aa:bb"}
        pre_row = _device_row(device_id="dev-1", ota_status="preauthorized", identity=identity)
        accepted_row = {**pre_row, "ota_status": "accepted"}
        # 1) lookup existing -> preauthorized, 2) update -> accepted
        mock_supabase.table.side_effect = _seq_tables([[pre_row], [accepted_row]])

        result = await register_device(
            RegisterDeviceParams(device_id="dev-1", identity_attributes=identity)
        )
        assert result.error is None
        assert result.device is not None
        assert result.device.status == "accepted"
        assert result.device.id == "dev-1"


class TestAcceptRejectDevice:
    async def test_accept_pending_device(self, mock_supabase):
        row = _device_row(ota_status="pending")
        accepted = {**row, "ota_status": "accepted"}
        mock_supabase.table.side_effect = _seq_tables([[row], [accepted]])

        result = await accept_device("dev-1")
        assert result.error is None
        assert result.device.status == "accepted"

    async def test_reject_accepted_device(self, mock_supabase):
        row = _device_row(ota_status="accepted")
        rejected = {**row, "ota_status": "rejected"}
        mock_supabase.table.side_effect = _seq_tables([[row], [rejected]])

        result = await reject_device("dev-1")
        assert result.error is None
        assert result.device.status == "rejected"

    async def test_illegal_transition_returns_error(self, mock_supabase):
        row = _device_row(ota_status="decommissioned")
        mock_supabase.table.side_effect = _seq_tables([[row]])

        result = await accept_device("dev-1")
        assert result.error is not None
        assert "Illegal device transition" in result.error

    async def test_device_not_found_returns_error(self, mock_supabase):
        mock_supabase.table.side_effect = _seq_tables([[]])

        result = await accept_device("no-such-id")
        assert result.error is not None
        assert "not found" in result.error


class TestPreauthorizeDevice:
    async def test_creates_preauthorized_device(self, mock_supabase):
        row = _device_row(ota_status="preauthorized")
        # 1) lookup existing -> none, 2) insert -> preauthorized row
        mock_supabase.table.side_effect = _seq_tables([[], [row]])

        result = await preauthorize_device(
            PreauthorizeDeviceParams(device_id="dev-1", identity_attributes={"mac": "aa:bb"})
        )
        assert result.error is None
        assert result.device.status == "preauthorized"


class TestDecommissionDevice:
    async def test_decommission_accepted_device(self, mock_supabase):
        row = _device_row(ota_status="accepted")
        decomm = {**row, "ota_status": "decommissioned"}
        # 1) transition select, 2) transition update, 3) ota_jobs update
        mock_supabase.table.side_effect = _seq_tables([[row], [decomm], []])

        result = await decommission_device("dev-1")
        assert result.error is None
        assert result.device.status == "decommissioned"


class TestListDevices:
    async def test_list_all(self, mock_supabase):
        rows = [_device_row("d1", "accepted"), _device_row("d2", "pending")]
        mock_supabase.table.side_effect = _seq_tables([rows])

        result = await list_devices(ListDevicesParams())
        assert len(result) == 2

    async def test_filter_by_status(self, mock_supabase):
        rows = [_device_row("d1", "accepted")]
        mock_supabase.table.side_effect = _seq_tables([rows])

        result = await list_devices(ListDevicesParams(status="accepted"))
        assert all(d.status == "accepted" for d in result)
