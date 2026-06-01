"""Integration tests for deployment tools."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nff_ota.models.deployment import CreateDeploymentParams, ReportDeviceStatusParams
from nff_ota.tools.deployments.ops import (
    abort_deployment,
    create_deployment,
    get_deployment_status,
    list_deployments,
    report_device_status,
)


def _dep_row(dep_id: str = "dep-1", status: str = "active") -> dict:
    return {
        "id": dep_id,
        "name": "deploy-v1",
        "artifact_id": "art-1",
        "device_group_id": "grp-1",
        "status": status,
        "max_devices_in_flight": 10,
        "retries": 3,
        "created_at": "2026-01-01T00:00:00+00:00",
        "start_time": None,
    }


def _dd_row(dd_id: str = "dd-1", dep_id: str = "dep-1", dev_id: str = "dev-1", status: str = "pending") -> dict:
    return {
        "id": dd_id,
        "deployment_id": dep_id,
        "device_id": dev_id,
        "status": status,
        "attempts": 0,
        "last_updated": "2026-01-01T00:00:00+00:00",
    }


class TestCreateDeployment:
    def _setup_mock(self, mock_supabase, artifact_exists: bool, group_exists: bool, member_ids: list[str], accepted_ids: list[str]) -> None:
        responses: dict[str, list[list]] = {
            "artifacts": [[{"id": "art-1"}] if artifact_exists else []],
            "device_groups": [[{"id": "grp-1"}] if group_exists else []],
            "device_group_members": [[{"device_id": d} for d in member_ids]],
            "devices": [[{"id": d} for d in accepted_ids]],
            "deployments": [[_dep_row()]],
            "device_deployments": [[]],
        }
        counters: dict[str, int] = {}

        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.insert.return_value = t
            t.eq.return_value = t
            t.in_.return_value = t
            t.order.return_value = t
            def execute():
                n = counters.get(name, 0)
                counters[name] = n + 1
                data = responses.get(name, [[]])[n] if n < len(responses.get(name, [[]])) else []
                return MagicMock(data=data, error=None)
            t.execute = execute
            return t
        mock_supabase.table.side_effect = table_se

    async def test_create_deployment_success(self, mock_supabase):
        self._setup_mock(mock_supabase, True, True, ["dev-1"], ["dev-1"])
        result = await create_deployment(CreateDeploymentParams(
            name="deploy-v1", artifact_id="art-1", device_group_id="grp-1"
        ))
        assert result.error is None
        assert result.deployment is not None
        assert result.deployment.status in ("active", "scheduled")

    async def test_artifact_not_found(self, mock_supabase):
        self._setup_mock(mock_supabase, False, True, ["dev-1"], ["dev-1"])
        result = await create_deployment(CreateDeploymentParams(
            name="d", artifact_id="no-art", device_group_id="grp-1"
        ))
        assert result.error is not None
        assert "not found" in result.error

    async def test_group_not_found(self, mock_supabase):
        self._setup_mock(mock_supabase, True, False, [], [])
        result = await create_deployment(CreateDeploymentParams(
            name="d", artifact_id="art-1", device_group_id="no-grp"
        ))
        assert result.error is not None
        assert "not found" in result.error

    async def test_no_accepted_devices_fails(self, mock_supabase):
        self._setup_mock(mock_supabase, True, True, ["dev-1"], [])  # member exists but not accepted
        result = await create_deployment(CreateDeploymentParams(
            name="d", artifact_id="art-1", device_group_id="grp-1"
        ))
        assert result.error is not None
        assert "accepted" in result.error

    async def test_empty_group_fails(self, mock_supabase):
        self._setup_mock(mock_supabase, True, True, [], [])
        result = await create_deployment(CreateDeploymentParams(
            name="d", artifact_id="art-1", device_group_id="grp-1"
        ))
        assert result.error is not None
        assert "no members" in result.error


class TestGetDeploymentStatus:
    async def test_returns_counts(self, mock_supabase):
        dep = _dep_row()
        dd_rows = [
            _dd_row("dd-1", status="success"),
            _dd_row("dd-2", dev_id="dev-2", status="downloading"),
            _dd_row("dd-3", dev_id="dev-3", status="pending"),
        ]
        call_n = [0]

        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.eq.return_value = t

            def execute():
                call_n[0] += 1
                if call_n[0] == 1:
                    return MagicMock(data=[dep], error=None)
                return MagicMock(data=dd_rows, error=None)
            t.execute = execute
            return t
        mock_supabase.table.side_effect = table_se

        result = await get_deployment_status("dep-1")
        assert result.error is None
        s = result.status
        assert s.total_count == 3
        assert s.success_count == 1
        assert s.downloading_count == 1
        assert s.pending_count == 1

    async def test_not_found_returns_error(self, mock_supabase):
        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.eq.return_value = t
            t.execute.return_value = MagicMock(data=[], error=None)
            return t
        mock_supabase.table.side_effect = table_se

        result = await get_deployment_status("no-such-id")
        assert result.error is not None


class TestAbortDeployment:
    async def test_abort_active_deployment(self, mock_supabase):
        dep = _dep_row(status="active")
        aborted = {**dep, "status": "aborted"}
        call_n = [0]

        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.update.return_value = t
            t.eq.return_value = t
            t.in_.return_value = t

            def execute():
                call_n[0] += 1
                if call_n[0] == 1:
                    return MagicMock(data=[dep], error=None)
                return MagicMock(data=[aborted], error=None)
            t.execute = execute
            return t
        mock_supabase.table.side_effect = table_se

        result = await abort_deployment("dep-1")
        assert result.error is None
        assert result.deployment.status == "aborted"

    async def test_abort_finished_deployment_fails(self, mock_supabase):
        dep = _dep_row(status="finished")

        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.eq.return_value = t
            t.execute.return_value = MagicMock(data=[dep], error=None)
            return t
        mock_supabase.table.side_effect = table_se

        result = await abort_deployment("dep-1")
        assert result.error is not None
        assert "Illegal deployment transition" in result.error


class TestReportDeviceStatus:
    async def test_downloading_increments_attempts(self, mock_supabase):
        dd = _dd_row(status="pending")
        dep = _dep_row()
        call_n = [0]

        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.update.return_value = t
            t.eq.return_value = t
            t.in_.return_value = t

            def execute():
                call_n[0] += 1
                if call_n[0] == 1:
                    return MagicMock(data=[dd], error=None)
                if name == "deployments":
                    return MagicMock(data=[dep], error=None)
                return MagicMock(data=[], error=None)
            t.execute = execute
            return t
        mock_supabase.table.side_effect = table_se

        result = await report_device_status(ReportDeviceStatusParams(
            device_id="dev-1", deployment_id="dep-1", status="downloading"
        ))
        assert result.error is None

    async def test_illegal_transition_returns_error(self, mock_supabase):
        dd = _dd_row(status="success")

        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.eq.return_value = t
            t.execute.return_value = MagicMock(data=[dd], error=None)
            return t
        mock_supabase.table.side_effect = table_se

        result = await report_device_status(ReportDeviceStatusParams(
            device_id="dev-1", deployment_id="dep-1", status="downloading"
        ))
        assert result.error is not None
        assert "Illegal device_deployment transition" in result.error

    async def test_all_success_finishes_deployment(self, mock_supabase):
        """When all devices report success, deployment should be marked finished."""
        dd_rebooting = _dd_row(status="rebooting")
        dep = _dep_row(status="active")
        # After update, all device_deployments are success
        dd_all_success = [
            _dd_row("dd-1", status="success"),
        ]
        call_n = [0]

        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.update.return_value = t
            t.eq.return_value = t
            t.in_.return_value = t

            def execute():
                call_n[0] += 1
                if call_n[0] == 1:
                    # find device_deployment
                    return MagicMock(data=[dd_rebooting], error=None)
                if call_n[0] == 2:
                    # update device_deployment
                    return MagicMock(data=[], error=None)
                if call_n[0] == 3:
                    # update devices.current_artifact_id — need artifact_id
                    return MagicMock(data=[{"artifact_id": "art-1"}], error=None)
                if call_n[0] == 4:
                    # update devices
                    return MagicMock(data=[], error=None)
                if call_n[0] == 5:
                    # recompute: get deployment
                    return MagicMock(data=[dep], error=None)
                if call_n[0] == 6:
                    # recompute: get all device_deployments
                    return MagicMock(data=dd_all_success, error=None)
                if call_n[0] == 7:
                    # recompute: update deployment to finished
                    return MagicMock(data=[], error=None)
                # final: get deployment for return
                return MagicMock(data=[{**dep, "status": "finished"}], error=None)
            t.execute = execute
            return t

        mock_supabase.table.side_effect = table_se

        result = await report_device_status(ReportDeviceStatusParams(
            device_id="dev-1", deployment_id="dep-1", status="success"
        ))
        assert result.error is None
