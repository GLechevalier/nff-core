"""Integration tests for phased rollout: confirm gate, supervisor advance/halt/wait, rate
limit, resume. Uses a recording table mock so writes (update/insert payloads) can be asserted."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from nff_ota import supervisor
from nff_ota.models.deployment import CreateDeploymentParams
from nff_ota.tools.deployments import ops
from nff_ota.tools.deployments.ops import (
    _rate_limited_devices,
    create_deployment,
    resume_deployment,
)


def _recording_tables(responses: dict[str, list[list[dict]]]):
    """Like _named_tables, but also records every .update()/.insert() payload per table."""
    counters: dict[str, int] = {}
    writes: dict[str, list[tuple[str, dict]]] = {"update": [], "insert": []}

    def side_effect(name):
        t = MagicMock()
        for m in ("select", "eq", "in_", "neq", "order", "limit", "lt", "lte", "gt", "is_"):
            getattr(t, m).return_value = t
        # PostgREST's negation reads `.not_.is_(col, val)`; `not_` is a property returning the
        # same builder, so mirror the chain back onto t.
        t.not_ = t

        def _update(payload):
            writes["update"].append((name, payload))
            return t

        def _insert(payload):
            writes["insert"].append((name, payload))
            return t

        t.update.side_effect = _update
        t.insert.side_effect = _insert

        async def execute():
            seq = responses.get(name, [[]])
            i = counters.get(name, 0)
            counters[name] = i + 1
            return MagicMock(data=(seq[i] if i < len(seq) else []), error=None)

        t.execute = execute
        return t

    return side_effect, writes


def _dep(**kw) -> dict:
    base = {
        "id": "dep-1", "name": "d", "target_version": "1.0.0", "device_group_id": "grp-1",
        "status": "active", "max_devices_in_flight": 10, "retries": 3,
        "created_at": "2026-01-01T00:00:00+00:00", "start_time": None,
        "phases": "50,100", "current_phase": 0, "phase_entered_at": None,
        "failure_threshold": 0.05, "soak_seconds": 0, "halted_reason": None,
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# Blast-radius confirmation gate (gap #3)                                       #
# --------------------------------------------------------------------------- #

class TestConfirmGate:
    async def test_large_target_requires_confirm(self, mock_supabase, monkeypatch):
        big = [{"id": f"d{i}"} for i in range(60)]  # > default threshold of 50
        monkeypatch.setattr(ops, "_resolve_deployable_devices", AsyncMock(return_value=(big, None)))
        se, _ = _recording_tables({
            "firmware_versions": [[{"version": "1.0.0", "storage_path": "fw/1.0.0.bin"}]],
            "device_groups": [[{"id": "grp-1", "project_id": "proj-1"}]],
        })
        mock_supabase.table.side_effect = se

        res = await create_deployment(CreateDeploymentParams(
            name="big", target_version="1.0.0", device_group_id="grp-1", phases=[5, 100]
        ))
        assert res.error is not None
        assert "confirm=true" in res.error

    async def test_confirm_true_persists_phases_and_waves(self, mock_supabase, monkeypatch):
        devices = [{"id": f"d{i}"} for i in range(60)]
        monkeypatch.setattr(ops, "_resolve_deployable_devices", AsyncMock(return_value=(devices, None)))
        insert_jobs = AsyncMock(return_value=60)
        monkeypatch.setattr(ops, "_insert_jobs", insert_jobs)
        se, writes = _recording_tables({
            "firmware_versions": [[{"version": "1.0.0", "storage_path": "fw/1.0.0.bin"}]],
            "device_groups": [[{"id": "grp-1", "project_id": "proj-1"}]],
            "deployments": [[]],  # insert response
        })
        mock_supabase.table.side_effect = se

        res = await create_deployment(CreateDeploymentParams(
            name="big", target_version="1.0.0", device_group_id="grp-1",
            phases=[5, 100], confirm=True,
        ))
        assert res.error is None
        # The deployment row stored the CSV phases; jobs were bucketed by the same phases.
        dep_inserts = [p for (tbl, p) in writes["insert"] if tbl == "deployments"]
        assert dep_inserts and dep_inserts[0]["phases"] == "5,100"
        assert insert_jobs.await_args.args[-1] == [5, 100]


# --------------------------------------------------------------------------- #
# Supervisor: advance / halt / wait                                            #
# --------------------------------------------------------------------------- #

class TestSupervisor:
    async def test_halts_when_failure_rate_exceeds_threshold(self, mock_supabase):
        dep = _dep(phases="50,100", current_phase=0, failure_threshold=0.05)
        jobs = [{"status": "success", "wave": 0},
                {"status": "failure", "wave": 0},
                {"status": "success", "wave": 0}]  # 1/3 = 33% > 5%
        se, writes = _recording_tables({"deployments": [[dep]], "ota_jobs": [[*jobs]]})
        mock_supabase.table.side_effect = se

        n = await supervisor.sweep_once()
        assert n == 1
        dep_updates = [p for (tbl, p) in writes["update"] if tbl == "deployments"]
        assert dep_updates and dep_updates[0]["status"] == "paused"
        assert "auto-halt" in dep_updates[0]["halted_reason"]

    async def test_advances_when_cohort_healthy_and_soaked(self, mock_supabase, monkeypatch):
        deliver = AsyncMock(return_value=MagicMock(delivered=5, failed=0, skipped=0))
        monkeypatch.setattr(supervisor, "_deliver_wave", deliver)
        dep = _dep(phases="50,100", current_phase=0, soak_seconds=0)
        jobs = [{"status": "success", "wave": 0}, {"status": "success", "wave": 0}]
        se, writes = _recording_tables({"deployments": [[dep]], "ota_jobs": [[*jobs]]})
        mock_supabase.table.side_effect = se

        await supervisor.sweep_once()
        dep_updates = [p for (tbl, p) in writes["update"] if tbl == "deployments"]
        assert dep_updates and dep_updates[0]["current_phase"] == 1
        deliver.assert_awaited_once()
        assert deliver.await_args.args[2] == 1  # max_wave == new phase

    async def test_waits_during_soak(self, mock_supabase, monkeypatch):
        deliver = AsyncMock()
        monkeypatch.setattr(supervisor, "_deliver_wave", deliver)
        # soak not elapsed: entered just now, soak_seconds large.
        entered = datetime.now(timezone.utc).isoformat()
        dep = _dep(phases="50,100", current_phase=0, soak_seconds=99999, phase_entered_at=entered)
        jobs = [{"status": "success", "wave": 0}, {"status": "success", "wave": 0}]
        se, writes = _recording_tables({"deployments": [[dep]], "ota_jobs": [[*jobs]]})
        mock_supabase.table.side_effect = se

        await supervisor.sweep_once()
        assert [p for (tbl, p) in writes["update"] if tbl == "deployments"] == []
        deliver.assert_not_awaited()

    async def test_ignores_non_phased_deployments(self, mock_supabase):
        dep = _dep(phases=None)
        se, writes = _recording_tables({"deployments": [[dep]]})
        mock_supabase.table.side_effect = se
        # phases is falsy → filtered out of the active-phased set entirely.
        n = await supervisor.sweep_once()
        assert n == 0


# --------------------------------------------------------------------------- #
# Rate limit + resume                                                          #
# --------------------------------------------------------------------------- #

class TestRateLimit:
    async def test_skips_device_with_recent_other_deployment(self, mock_supabase):
        rows = [
            {"device_id": "d1", "deployment_id": "other", "status": "downloading", "last_updated": "x"},
            {"device_id": "d2", "deployment_id": "dep-1", "status": "downloading", "last_updated": "x"},
            {"device_id": "d3", "deployment_id": "other", "status": "pending", "last_updated": "x"},
        ]
        se, _ = _recording_tables({"ota_jobs": [[*rows]]})
        mock_supabase.table.side_effect = se
        limited = await _rate_limited_devices(["d1", "d2", "d3"], exclude_deployment="dep-1")
        assert limited == {"d1"}  # d2 same deployment, d3 still pending (never sent)


class TestResume:
    async def test_resume_paused_to_active(self, mock_supabase):
        dep = _dep(status="paused", halted_reason="auto-halt ...")
        se, writes = _recording_tables({"deployments": [[dep]]})
        mock_supabase.table.side_effect = se
        res = await resume_deployment("dep-1")
        assert res.error is None
        upd = [p for (tbl, p) in writes["update"] if tbl == "deployments"]
        assert upd and upd[0]["status"] == "active" and upd[0]["halted_reason"] is None
