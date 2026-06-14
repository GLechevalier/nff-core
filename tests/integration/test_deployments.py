"""Integration tests for deployment tools."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nff_ota import fleet
from nff_ota.models.deployment import CreateDeploymentParams, ReportDeviceStatusParams
from nff_ota.tools.deployments.ops import (
    _filter_compatible,
    _materialize_jobs,
    abort_deployment,
    create_deployment,
    get_deployment_status,
    list_deployments,
    report_device_status,
    start_deployment,
)


def _named_tables(responses: dict[str, list[list[dict]]]):
    """side_effect for client.table(name): per-table sequential awaited .execute() responses."""
    counters: dict[str, int] = {}

    def side_effect(name):
        t = MagicMock()
        for m in ("select", "insert", "upsert", "update", "delete", "eq", "in_", "neq", "order", "limit", "lt", "gt"):
            getattr(t, m).return_value = t

        async def execute():
            seq = responses.get(name, [[]])
            i = counters.get(name, 0)
            counters[name] = i + 1
            return MagicMock(data=(seq[i] if i < len(seq) else []), error=None)

        t.execute = execute
        return t

    return side_effect


class TestStartDeploymentWireVersion:
    """start_deployment must send the artifact's semver release_version on the wire (the
    device's anti-downgrade gate needs a semver), not the catalog target_version."""

    def _run(self, mock_supabase, monkeypatch, fw_row):
        dep = _dep_row(status="active")
        dep["target_version"] = "repo-abc1234"
        job = _job_row(status="pending")
        job["target_version"] = "repo-abc1234"
        mock_supabase.table.side_effect = _named_tables({
            "deployments": [[dep], [dep], [dep]],         # initial, recompute, final
            "firmware_versions": [[fw_row]],
            "ota_jobs": [[job], [], [{"status": "downloading"}]],  # pending select, update, recompute
            "devices": [[{"id": "dev-1", "identity_attributes": {"mqtt_id": "dev-1"}, "project_id": "proj-1"}]],
        })
        monkeypatch.setattr(
            "nff_ota.storage.create_signed_url", AsyncMock(return_value="https://signed/fw.bin")
        )
        deliver = AsyncMock(return_value={"status": "ota_started"})
        monkeypatch.setattr(fleet, "deliver_ota", deliver)
        return deliver

    async def test_sends_release_version_semver(self, mock_supabase, monkeypatch):
        fw = {"version": "repo-abc1234", "checksum_sha256": "deadbeef", "release_version": "3.0.13"}
        deliver = self._run(mock_supabase, monkeypatch, fw)
        await start_deployment("dep-1")
        deliver.assert_awaited_once()
        # deliver_ota(mqtt_id, version, sha256, url) — version is the 2nd positional arg.
        assert deliver.await_args.args[1] == "3.0.13"

    async def test_falls_back_to_target_version_when_no_release(self, mock_supabase, monkeypatch):
        fw = {"version": "repo-abc1234", "checksum_sha256": "deadbeef", "release_version": None}
        deliver = self._run(mock_supabase, monkeypatch, fw)
        await start_deployment("dep-1")
        assert deliver.await_args.args[1] == "repo-abc1234"


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


class TestFilterCompatible:
    """Unit coverage of the pure device_type gate logic."""

    def _dev(self, dev_id: str, device_type):
        return {"id": dev_id, "device_type": device_type}

    def test_no_artifact_types_is_ungated(self):
        devs = [self._dev("a", "esp32"), self._dev("b", "esp32s3")]
        compatible, skipped = _filter_compatible(devs, [])
        assert compatible == devs
        assert skipped == []

    def test_matching_type_is_compatible(self):
        devs = [self._dev("a", "esp32")]
        compatible, skipped = _filter_compatible(devs, ["esp32"])
        assert [d["id"] for d in compatible] == ["a"]
        assert skipped == []

    def test_mismatched_type_is_skipped(self):
        devs = [self._dev("a", "esp32"), self._dev("b", "esp32s3")]
        compatible, skipped = _filter_compatible(devs, ["esp32s3"])
        assert [d["id"] for d in compatible] == ["b"]
        assert [d["id"] for d in skipped] == ["a"]

    def test_unknown_type_allowed_with_warning(self):
        devs = [self._dev("legacy", None), self._dev("blank", "")]
        compatible, skipped = _filter_compatible(devs, ["esp32"])
        assert [d["id"] for d in compatible] == ["legacy", "blank"]
        assert skipped == []

    def test_match_is_case_insensitive(self):
        devs = [self._dev("a", "ESP32")]
        compatible, skipped = _filter_compatible(devs, ["esp32"])
        assert [d["id"] for d in compatible] == ["a"]
        assert skipped == []


class TestDeployGate:
    """The deploy-time gate as exercised through create_deployment / poller."""

    def _setup(self, mock_supabase, *, fw_device_types, devices, members=None, for_materialize=False):
        """Wire a mock client and capture inserted ota_jobs rows.

        ``devices`` is a list of {id, device_type}. ``create_deployment`` queries
        firmware_versions twice (storage_path, then device_types); the poller path
        (``for_materialize``) queries it once (device_types only).
        """
        if members is None:
            members = [d["id"] for d in devices]
        captured: dict[str, list] = {"jobs": []}
        fw_storage_row = {"version": "1.0.0", "storage_path": "firmware/1.0.0.bin"}
        fw_types_row = {"device_types": fw_device_types}
        fw_seq = [[fw_types_row]] if for_materialize else [[fw_storage_row], [fw_types_row]]
        responses: dict[str, list[list]] = {
            "firmware_versions": fw_seq,
            "device_groups": [[{"id": "grp-1"}]],
            "device_group_members": [[{"device_id": m} for m in members]],
            "devices": [[
                {
                    "id": d["id"],
                    "current_firmware_version": "0.9.0",
                    "firmware_version": "0.9.0",
                    "device_type": d.get("device_type"),
                }
                for d in devices
            ]],
            "deployments": [[_dep_row()]],
            "ota_jobs": [[]],
        }
        counters: dict[str, int] = {}

        def table_se(name):
            t = MagicMock()
            t.select.return_value = t
            t.eq.return_value = t
            t.in_.return_value = t
            t.order.return_value = t

            def _insert(rows):
                if name == "ota_jobs":
                    captured["jobs"].extend(rows)
                return t
            t.insert.side_effect = _insert

            async def execute():
                n = counters.get(name, 0)
                counters[name] = n + 1
                seq = responses.get(name, [[]])
                data = seq[n] if n < len(seq) else []
                return MagicMock(data=data, error=None)
            t.execute = execute
            return t
        mock_supabase.table.side_effect = table_se
        return captured

    async def test_compatible_device_deploys(self, mock_supabase):
        captured = self._setup(
            mock_supabase, fw_device_types=["esp32"], devices=[{"id": "dev-1", "device_type": "esp32"}]
        )
        result = await create_deployment(CreateDeploymentParams(
            name="d", target_version="1.0.0", device_group_id="grp-1"
        ))
        assert result.error is None
        assert [j["device_id"] for j in captured["jobs"]] == ["dev-1"]

    async def test_incompatible_device_excluded(self, mock_supabase):
        captured = self._setup(
            mock_supabase,
            fw_device_types=["esp32s3"],
            devices=[{"id": "dev-32", "device_type": "esp32"}, {"id": "dev-s3", "device_type": "esp32s3"}],
        )
        result = await create_deployment(CreateDeploymentParams(
            name="d", target_version="1.0.0", device_group_id="grp-1"
        ))
        assert result.error is None
        assert [j["device_id"] for j in captured["jobs"]] == ["dev-s3"]

    async def test_unknown_device_type_allowed(self, mock_supabase):
        captured = self._setup(
            mock_supabase, fw_device_types=["esp32"], devices=[{"id": "legacy", "device_type": None}]
        )
        result = await create_deployment(CreateDeploymentParams(
            name="d", target_version="1.0.0", device_group_id="grp-1"
        ))
        assert result.error is None
        assert [j["device_id"] for j in captured["jobs"]] == ["legacy"]

    async def test_zero_compatible_returns_error(self, mock_supabase):
        captured = self._setup(
            mock_supabase, fw_device_types=["esp32s3"], devices=[{"id": "dev-32", "device_type": "esp32"}]
        )
        result = await create_deployment(CreateDeploymentParams(
            name="d", target_version="1.0.0", device_group_id="grp-1"
        ))
        assert result.error is not None
        assert "compatible" in result.error
        assert captured["jobs"] == []

    async def test_empty_artifact_types_ungated(self, mock_supabase):
        captured = self._setup(
            mock_supabase, fw_device_types=[], devices=[{"id": "dev-s3", "device_type": "esp32s3"}]
        )
        result = await create_deployment(CreateDeploymentParams(
            name="d", target_version="1.0.0", device_group_id="grp-1"
        ))
        assert result.error is None
        assert [j["device_id"] for j in captured["jobs"]] == ["dev-s3"]

    async def test_materialize_jobs_applies_gate(self, mock_supabase):
        captured = self._setup(
            mock_supabase,
            fw_device_types=["esp32s3"],
            devices=[{"id": "dev-32", "device_type": "esp32"}, {"id": "dev-s3", "device_type": "esp32s3"}],
            for_materialize=True,
        )
        count, err = await _materialize_jobs("dep-1", "1.0.0", "grp-1")
        assert err is None
        assert count == 1
        assert [j["device_id"] for j in captured["jobs"]] == ["dev-s3"]


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
