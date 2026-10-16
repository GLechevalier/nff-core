"""Integration tests for the OTA-result feedback loop: finalize_ota_result + reaper."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nff_ota import reaper
from nff_ota.tools.deployments import ops
from nff_ota.tools.deployments.ops import finalize_ota_result


def _job_row(status: str = "downloading", dev_id: str = "test-device-01", version: str = "2.0.0") -> dict:
    return {
        "id": "job-1",
        "deployment_id": "dep-1",
        "device_id": dev_id,
        "from_version": "1.0.0",
        "target_version": version,
        "status": status,
        "progress": 0,
        "attempts": 1,
        "started_at": "2026-01-01T00:00:00+00:00",
        "last_updated": "2026-01-01T00:00:00+00:00",
    }


def _dep_row(status: str = "active") -> dict:
    return {
        "id": "dep-1", "name": "d", "target_version": "2.0.0", "device_group_id": "grp-1",
        "status": status, "max_devices_in_flight": 1, "retries": 3,
        "created_at": "2026-01-01T00:00:00+00:00", "start_time": None,
    }


def _named_tables(responses: dict[str, list[list[dict]]]) -> MagicMock:
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


class TestFinalizeOtaResult:
    async def test_committed_marks_success(self, mock_supabase):
        mock_supabase.table.side_effect = _named_tables({
            "devices": [[{"id": "test-device-01"}], []],          # resolve, then current_fw update
            "ota_jobs": [[_job_row("downloading")], [], [{"status": "success"}]],  # select, update, recompute
            "deployments": [[{"target_version": "2.0.0"}], [_dep_row()], []],      # target_version, recompute*, finish
        })
        result = await finalize_ota_result("test-device-01", "committed", "2.0.0")
        assert result["finalized"] is True
        assert result["status"] == "success"
        assert result["device_id"] == "test-device-01"
        assert result["deployment_id"] == "dep-1"

    async def test_rolled_back_marks_failure(self, mock_supabase):
        mock_supabase.table.side_effect = _named_tables({
            "devices": [[{"id": "test-device-01"}]],              # resolve only (no fw update on failure)
            "ota_jobs": [[_job_row("downloading")], [], [{"status": "failure"}]],
            "deployments": [[_dep_row()], []],                    # recompute select*, finish
        })
        result = await finalize_ota_result("test-device-01", "rolled_back", "2.0.0")
        assert result["finalized"] is True
        assert result["status"] == "failure"

    async def test_matches_via_fallback_when_reported_version_differs(self, mock_supabase):
        # Connector case: the job's target_version is the catalog id ("repo-abc1234") but the
        # device echoes the semver wire version ("3.0.13"). The exact target_version match
        # misses; the most-recent-active-job fallback still finalizes it.
        job = _job_row("downloading", version="repo-abc1234")
        mock_supabase.table.side_effect = _named_tables({
            "devices": [[{"id": "test-device-01"}], []],
            # exact select (miss) -> fallback select (hit) -> update -> recompute select
            "ota_jobs": [[], [job], [], [{"status": "success"}]],
            "deployments": [[{"target_version": "repo-abc1234"}], [_dep_row()], []],
        })
        result = await finalize_ota_result("test-device-01", "committed", "3.0.13")
        assert result["finalized"] is True
        assert result["status"] == "success"

    async def test_already_terminal_is_noop(self, mock_supabase):
        mock_supabase.table.side_effect = _named_tables({
            "devices": [[{"id": "test-device-01"}]],
            "ota_jobs": [[_job_row("success")]],                  # already terminal → filtered out
        })
        result = await finalize_ota_result("test-device-01", "committed", "2.0.0")
        assert result["finalized"] is False
        assert result["reason"] == "no-active-job"

    async def test_unknown_device_is_noop(self, mock_supabase):
        mock_supabase.table.side_effect = _named_tables({
            "devices": [[], []],                                  # direct miss, mqtt_id miss
        })
        result = await finalize_ota_result("ghost", "committed", "2.0.0")
        assert result["finalized"] is False
        assert result["reason"] == "device-not-found"

    async def test_does_not_validate_transition(self, mock_supabase, monkeypatch):
        # The finalizer must bypass step validation (job sits in 'downloading', and
        # downloading->success is intentionally illegal). If it called validate_transition
        # this would raise; assert it still succeeds.
        def _boom(*a, **k):
            raise AssertionError("validate_transition must not be called by finalize_ota_result")
        monkeypatch.setattr(ops, "validate_transition", _boom)
        mock_supabase.table.side_effect = _named_tables({
            "devices": [[{"id": "test-device-01"}], []],
            "ota_jobs": [[_job_row("downloading")], [], [{"status": "success"}]],
            "deployments": [[{"target_version": "2.0.0"}], [_dep_row()], []],
        })
        result = await finalize_ota_result("test-device-01", "committed", "2.0.0")
        assert result["finalized"] is True


class TestReaper:
    async def test_sweep_times_out_stuck_job(self, mock_supabase):
        mock_supabase.table.side_effect = _named_tables({
            # sweep select (downloading+stale), update->timed_out, then recompute status-select
            "ota_jobs": [[_job_row("downloading")], [], [{"status": "timed_out"}]],
            "deployments": [[_dep_row()], []],                    # recompute select*, finish
        })
        reaped = await reaper.sweep_once()
        assert reaped == 1

    async def test_sweep_noop_when_none_stuck(self, mock_supabase):
        mock_supabase.table.side_effect = _named_tables({"ota_jobs": [[]]})
        reaped = await reaper.sweep_once()
        assert reaped == 0
