"""Integration tests for device tools using a mocked Supabase client."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

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
    status: str = "pending",
    identity: dict | None = None,
) -> dict:
    return {
        "id": device_id,
        "identity_attributes": identity or {"mac": "aa:bb"},
        "public_key": None,
        "status": status,
        "current_artifact_id": None,
        "tags": [],
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def _build_table_mock(rows_by_table: dict[str, list]) -> MagicMock:
    def side_effect(name):
        t = MagicMock()
        t.select.return_value = t
        t.insert.return_value = t
        t.update.return_value = t
        t.delete.return_value = t
        t.eq.return_value = t
        t.in_.return_value = t
        t.order.return_value = t
        data = rows_by_table.get(name, [])
        t.execute.return_value = MagicMock(data=data, error=None)
        return t
    return side_effect


class TestRegisterDevice:
    async def test_creates_pending_device(self, mock_supabase):
        row = _device_row()
        mock_supabase.table.side_effect = _build_table_mock({
            "devices": [row],  # preauth check returns empty, insert returns row
        })

        # First call (preauth check) returns empty; second (insert) returns row
        call_n = [0]
        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.insert.return_value = t
            t.update.return_value = t
            t.eq.return_value = t
            t.in_.return_value = t
            t.order.return_value = t
            call_n[0] += 1
            if call_n[0] == 1:
                # preauth query
                t.execute.return_value = MagicMock(data=[], error=None)
            else:
                t.execute.return_value = MagicMock(data=[row], error=None)
            return t
        mock_supabase.table.side_effect = table_se

        result = await register_device(RegisterDeviceParams(identity_attributes={"mac": "aa:bb"}))
        assert result.error is None
        assert result.device is not None
        assert result.device.status == "pending"

    async def test_preauth_match_auto_accepts(self, mock_supabase):
        identity = {"mac": "aa:bb"}
        pre_row = _device_row(device_id="pre-1", status="preauthorized", identity=identity)
        accepted_row = {**pre_row, "status": "accepted"}

        call_n = [0]
        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.insert.return_value = t
            t.update.return_value = t
            t.eq.return_value = t
            t.in_.return_value = t
            t.order.return_value = t
            call_n[0] += 1
            if call_n[0] == 1:
                # preauth query returns a match
                t.execute.return_value = MagicMock(data=[pre_row], error=None)
            else:
                # update call
                t.execute.return_value = MagicMock(data=[accepted_row], error=None)
            return t
        mock_supabase.table.side_effect = table_se

        result = await register_device(RegisterDeviceParams(identity_attributes=identity))
        assert result.error is None
        assert result.device is not None
        assert result.device.status == "accepted"
        assert result.device.id == "pre-1"


class TestAcceptRejectDevice:
    async def test_accept_pending_device(self, mock_supabase):
        row = _device_row(status="pending")
        accepted = {**row, "status": "accepted"}

        call_n = [0]
        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.update.return_value = t
            t.eq.return_value = t
            call_n[0] += 1
            if call_n[0] == 1:
                t.execute.return_value = MagicMock(data=[row], error=None)
            else:
                t.execute.return_value = MagicMock(data=[accepted], error=None)
            return t
        mock_supabase.table.side_effect = table_se

        result = await accept_device("dev-1")
        assert result.error is None
        assert result.device.status == "accepted"

    async def test_reject_accepted_device(self, mock_supabase):
        row = _device_row(status="accepted")
        rejected = {**row, "status": "rejected"}

        call_n = [0]
        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.update.return_value = t
            t.eq.return_value = t
            call_n[0] += 1
            t.execute.return_value = MagicMock(data=[row if call_n[0] == 1 else rejected], error=None)
            return t
        mock_supabase.table.side_effect = table_se

        result = await reject_device("dev-1")
        assert result.error is None
        assert result.device.status == "rejected"

    async def test_illegal_transition_returns_error(self, mock_supabase):
        row = _device_row(status="decommissioned")

        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.eq.return_value = t
            t.execute.return_value = MagicMock(data=[row], error=None)
            return t
        mock_supabase.table.side_effect = table_se

        result = await accept_device("dev-1")
        assert result.error is not None
        assert "Illegal device transition" in result.error

    async def test_device_not_found_returns_error(self, mock_supabase):
        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.eq.return_value = t
            t.execute.return_value = MagicMock(data=[], error=None)
            return t
        mock_supabase.table.side_effect = table_se

        result = await accept_device("no-such-id")
        assert result.error is not None
        assert "not found" in result.error


class TestPreauthorizeDevice:
    async def test_creates_preauthorized_device(self, mock_supabase):
        row = _device_row(status="preauthorized")

        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.insert.return_value = t
            t.eq.return_value = t
            t.execute.return_value = MagicMock(data=[row], error=None)
            return t
        mock_supabase.table.side_effect = table_se

        result = await preauthorize_device(PreauthorizeDeviceParams(identity_attributes={"mac": "aa:bb"}))
        assert result.error is None
        assert result.device.status == "preauthorized"


class TestDecommissionDevice:
    async def test_decommission_accepted_device(self, mock_supabase):
        row = _device_row(status="accepted")
        decomm = {**row, "status": "decommissioned"}

        call_n = [0]
        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.update.return_value = t
            t.eq.return_value = t
            t.in_.return_value = t
            call_n[0] += 1
            if call_n[0] == 1:
                t.execute.return_value = MagicMock(data=[row], error=None)
            else:
                t.execute.return_value = MagicMock(data=[decomm], error=None)
            return t
        mock_supabase.table.side_effect = table_se

        result = await decommission_device("dev-1")
        assert result.error is None
        assert result.device.status == "decommissioned"


class TestListDevices:
    async def test_list_all(self, mock_supabase):
        rows = [_device_row("d1", "accepted"), _device_row("d2", "pending")]
        mock_supabase.table.return_value.select.return_value.order.return_value.execute.return_value = MagicMock(data=rows, error=None)

        result = await list_devices(ListDevicesParams())
        assert len(result) == 2

    async def test_filter_by_status(self, mock_supabase):
        rows = [_device_row("d1", "accepted")]
        t = MagicMock()
        t.select.return_value = t
        t.order.return_value = t
        t.eq.return_value = t
        t.execute.return_value = MagicMock(data=rows, error=None)
        mock_supabase.table.return_value = t

        result = await list_devices(ListDevicesParams(status="accepted"))
        assert all(d.status == "accepted" for d in result)
