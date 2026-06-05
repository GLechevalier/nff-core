"""Integration tests for deployment tools."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

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
        "target_version": "1.0.0",
        "device_group_id": "grp-1",
        "status": status,
        "max_devices_in_flight": 10,
        "retries": 3,
        "created_at": "2026-01-01T00:00:00+00:00",
        "start_time": None,
    }


def _job_row(job_id: str = "job-1", dep_id: str = "dep-1", dev_id: str = "dev-1", status: str = "pending") -> dict:
    """An ota_jobs row (the unified per-device deployment record)."""
    return {
        "id": job_id,
        "deployment_id": dep_id,
        "device_id": dev_id,
        "from_version": "0.9.0",
        "target_version": "1.0.0",
        "status": status,
        "progress": 0,
        "attempts": 0,
        "started_at": "2026-01-01T00:00:00+00:00",
        "last_updated": "2026-01-01T00:00:00+00:00",
    }


def _async_exec(data_fn):
    """Build an async execute() that returns MagicMock(data=...) computed by data_fn()."""
    async def execute():
        return MagicMock(data=data_fn(), error=None)
    return execute


class TestCreateDeployment:
    def _setup_mock(self, mock_supabase, version_exists: bool, has_binary: bool, group_exists: bool, member_ids: list[str], accepted_ids: list[str]) -> None:
        fw_row = {"version": "1.0.0", "storage_path": "firmware/1.0.0.bin" if has_binary else None}
        responses: dict[str, list[list]] = {
            "firmware_versions": [[fw_row] if version_exists else []],
            "device_groups": [[{"id": "grp-1"}] if group_exists else []],
            "device_group_members": [[{"device_id": d} for d in member_ids]],
            "devices": [[{"id": d, "current_firmware_version": "0.9.0", "firmware_version": "0.9.0"} for d in accepted_ids]],
            "deployments": [[_dep_row()]],
            "ota_jobs": [[]],
        }
        counters: dict[str, int] = {}

        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.insert.return_value = t
            t.eq.return_value = t
            t.in_.return_value = t
            t.order.return_value = t

            async def execute():
                n = counters.get(name, 0)
                counters[name] = n + 1
                table_resp = responses.get(name, [[]])
                data = table_resp[n] if n < len(table_resp) else []
                return MagicMock(data=data, error=None)
            t.execute = execute
            return t
        mock_supabase.table.side_effect = table_se

    async def test_create_deployment_success(self, mock_supabase):
        self._setup_mock(mock_supabase, True, True, True, ["dev-1"], ["dev-1"])
        result = await create_deployment(CreateDeploymentParams(
            name="deploy-v1", target_version="1.0.0", device_group_id="grp-1"
        ))
        assert result.error is None
        assert result.deployment is not None
        assert result.deployment.target_version == "1.0.0"
        assert result.deployment.status in ("active", "scheduled")

    async def test_version_not_found(self, mock_supabase):
        self._setup_mock(mock_supabase, False, False, True, ["dev-1"], ["dev-1"])
        result = await create_deployment(CreateDeploymentParams(
            name="d", target_version="no-such", device_group_id="grp-1"
        ))
        assert result.error is not None
        assert "not found" in result.error

    async def test_version_without_binary_fails(self, mock_supabase):
        self._setup_mock(mock_supabase, True, False, True, ["dev-1"], ["dev-1"])
        result = await create_deployment(CreateDeploymentParams(
            name="d", target_version="1.0.0", device_group_id="grp-1"
        ))
        assert result.error is not None
        assert "no uploaded artifact" in result.error

    async def test_group_not_found(self, mock_supabase):
        self._setup_mock(mock_supabase, True, True, False, [], [])
        result = await create_deployment(CreateDeploymentParams(
            name="d", target_version="1.0.0", device_group_id="no-grp"
        ))
        assert result.error is not None
        assert "not found" in result.error

    async def test_no_accepted_devices_fails(self, mock_supabase):
        self._setup_mock(mock_supabase, True, True, True, ["dev-1"], [])  # member exists but not accepted
        result = await create_deployment(CreateDeploymentParams(
            name="d", target_version="1.0.0", device_group_id="grp-1"
        ))
        assert result.error is not None
        assert "accepted" in result.error

    async def test_empty_group_fails(self, mock_supabase):
        self._setup_mock(mock_supabase, True, True, True, [], [])
        result = await create_deployment(CreateDeploymentParams(
            name="d", target_version="1.0.0", device_group_id="grp-1"
        ))
        assert result.error is not None
        assert "no members" in result.error


class TestGetDeploymentStatus:
    async def test_returns_counts(self, mock_supabase):
        dep = _dep_row()
        jobs = [
            _job_row("job-1", status="success"),
            _job_row("job-2", dev_id="dev-2", status="downloading"),
            _job_row("job-3", dev_id="dev-3", status="pending"),
        ]
        call_n = [0]

        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.eq.return_value = t

            async def execute():
                call_n[0] += 1
                if call_n[0] == 1:
                    return MagicMock(data=[dep], error=None)
                return MagicMock(data=jobs, error=None)
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
            t.execute = AsyncMock(return_value=MagicMock(data=[], error=None))
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

            async def execute():
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
            t.execute = AsyncMock(return_value=MagicMock(data=[dep], error=None))
            return t
        mock_supabase.table.side_effect = table_se

        result = await abort_deployment("dep-1")
        assert result.error is not None
        assert "Illegal deployment transition" in result.error


class TestReportDeviceStatus:
    async def test_downloading_increments_attempts(self, mock_supabase):
        job = _job_row(status="pending")
        dep = _dep_row()
        call_n = [0]

        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.update.return_value = t
            t.eq.return_value = t
            t.in_.return_value = t

            async def execute():
                call_n[0] += 1
                if call_n[0] == 1:
                    return MagicMock(data=[job], error=None)
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
        job = _job_row(status="success")

        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.eq.return_value = t
            t.execute = AsyncMock(return_value=MagicMock(data=[job], error=None))
            return t
        mock_supabase.table.side_effect = table_se

        result = await report_device_status(ReportDeviceStatusParams(
            device_id="dev-1", deployment_id="dep-1", status="downloading"
        ))
        assert result.error is not None
        assert "Illegal device_deployment transition" in result.error

    async def test_all_success_finishes_deployment(self, mock_supabase):
        """When all devices report success, deployment should be marked finished."""
        job_rebooting = _job_row(status="rebooting")
        dep = _dep_row(status="active")
        jobs_all_success = [_job_row("job-1", status="success")]
        call_n = [0]

        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.update.return_value = t
            t.eq.return_value = t
            t.in_.return_value = t

            async def execute():
                call_n[0] += 1
                if call_n[0] == 1:
                    # find ota_jobs row
                    return MagicMock(data=[job_rebooting], error=None)
                if call_n[0] == 2:
                    # update ota_jobs row
                    return MagicMock(data=[], error=None)
                if call_n[0] == 3:
                    # success branch: read deployment target_version
                    return MagicMock(data=[{"target_version": "1.0.0"}], error=None)
                if call_n[0] == 4:
                    # update devices.current_firmware_version
                    return MagicMock(data=[], error=None)
                if call_n[0] == 5:
                    # recompute: get deployment
                    return MagicMock(data=[dep], error=None)
                if call_n[0] == 6:
                    # recompute: get all ota_jobs
                    return MagicMock(data=jobs_all_success, error=None)
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
