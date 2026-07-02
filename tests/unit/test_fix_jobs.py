"""Tests for the async firmware-fix job queue.

Covers submit_firmware_fix enqueuing (and its up-front validation gates), get_firmware_fix_status,
and the background fix_worker claiming + running + finishing a job. The compile+deploy core
(build_and_deploy_source) is stubbed so these run DB- and Docker-free.
"""
from unittest.mock import AsyncMock, MagicMock

from nff_ota.models.source import FirmwareFixJob, SubmitFirmwareFixResult
from nff_ota.source import fix_worker, jobs, ops

SRC = "void setup(){}\nvoid loop(){}\n"


def _resp(data):
    return MagicMock(data=data)


async def test_submit_enqueues_and_returns_pending(mock_supabase):
    # devices lookup → a tenant; latest revision → none; insert → ok.
    mock_supabase.table.return_value.execute = AsyncMock(
        side_effect=[_resp([{"project_id": "p1"}]), _resp([]), _resp([{"id": "j"}])]
    )
    res = await ops.submit_firmware_fix("dev-1", "2.0.0", SRC)
    assert isinstance(res, FirmwareFixJob)
    assert res.status == "pending"
    assert res.job_id  # a real uuid was minted
    assert res.device_id == "dev-1"
    assert res.version == "2.0.0"
    # The last table op was the job insert.
    mock_supabase.table.return_value.insert.assert_called()


async def test_submit_rejects_bad_semver_without_enqueue(mock_supabase):
    res = await ops.submit_firmware_fix("dev-1", "v2", SRC)
    assert res.status == "error"
    assert res.job_id == ""
    assert "semver" in res.error
    mock_supabase.table.return_value.insert.assert_not_called()


async def test_submit_rejects_downgrade_without_enqueue(mock_supabase):
    mock_supabase.table.return_value.execute = AsyncMock(
        side_effect=[_resp([{"project_id": "p1"}]), _resp([{"version": "5.0.0"}])]
    )
    res = await ops.submit_firmware_fix("dev-1", "4.0.0", SRC)
    assert res.status == "error"
    assert "not strictly greater" in res.error
    mock_supabase.table.return_value.insert.assert_not_called()


async def test_submit_rejects_unknown_device(mock_supabase):
    mock_supabase.table.return_value.execute = AsyncMock(side_effect=[_resp([])])
    res = await ops.submit_firmware_fix("ghost", "2.0.0", SRC)
    assert res.status == "error"
    assert "not found" in res.error
    mock_supabase.table.return_value.insert.assert_not_called()


async def test_get_status_reads_job(mock_supabase):
    row = {
        "id": "job-1", "status": "done", "device_id": "dev-1", "version": "2.0.0",
        "deployment_id": "dep-1", "fault_remaining": False, "error": None,
    }
    mock_supabase.table.return_value.execute = AsyncMock(return_value=_resp([row]))
    res = await ops.get_firmware_fix_status("job-1")
    assert res.status == "done"
    assert res.deployment_id == "dep-1"
    assert res.fault_remaining is False


async def test_get_status_missing_job(mock_supabase):
    mock_supabase.table.return_value.execute = AsyncMock(return_value=_resp([]))
    res = await ops.get_firmware_fix_status("nope")
    assert res.status == "error"
    assert "not found" in res.error


async def test_worker_claims_runs_and_finishes(mock_supabase, monkeypatch):
    job = {
        "id": "job-1", "device_id": "dev-1", "version": "2.0.0", "source": SRC,
        "author": "agent", "parent_version": "1.0.0", "changelog": None, "attempts": 0,
    }
    # reclaim_stale (running select) → none; claim select → the job; claim update → success;
    # finish update → ignored.
    mock_supabase.table.return_value.execute = AsyncMock(
        side_effect=[_resp([]), _resp([job]), _resp([job]), _resp([{}])]
    )

    seen = {}

    async def fake_build(device_id, version, source, author, **kw):
        seen["args"] = (device_id, version, source, author, kw.get("parent_version"))
        return SubmitFirmwareFixResult(
            deployment_id="dep-9", version=version, fault_remaining=False
        )

    monkeypatch.setattr(fix_worker, "build_and_deploy_source", fake_build)

    ran = await fix_worker.sweep_once()
    assert ran is True
    assert seen["args"] == ("dev-1", "2.0.0", SRC, "agent", "1.0.0")
    # The last write was jobs.finish → an update marking the job done.
    mock_supabase.table.return_value.update.assert_called()


async def test_worker_idle_when_no_pending(mock_supabase, monkeypatch):
    # reclaim_stale (running) → none; claim select → none.
    mock_supabase.table.return_value.execute = AsyncMock(side_effect=[_resp([]), _resp([])])
    called = {"n": 0}

    async def fake_build(*a, **k):
        called["n"] += 1
        return SubmitFirmwareFixResult()

    monkeypatch.setattr(fix_worker, "build_and_deploy_source", fake_build)
    ran = await fix_worker.sweep_once()
    assert ran is False
    assert called["n"] == 0  # nothing to run


async def test_worker_records_build_error(mock_supabase, monkeypatch):
    job = {
        "id": "job-1", "device_id": "dev-1", "version": "2.0.0", "source": SRC,
        "author": "agent", "parent_version": None, "changelog": None, "attempts": 0,
    }
    mock_supabase.table.return_value.execute = AsyncMock(
        side_effect=[_resp([]), _resp([job]), _resp([job]), _resp([{}])]
    )
    finished = {}

    async def fake_build(*a, **k):
        return SubmitFirmwareFixResult(error="real compile failed: boom")

    async def fake_finish(job_id, **kwargs):
        finished["kwargs"] = kwargs

    monkeypatch.setattr(fix_worker, "build_and_deploy_source", fake_build)
    monkeypatch.setattr(fix_worker.jobs, "finish", fake_finish)

    await fix_worker.sweep_once()
    assert finished["kwargs"]["status"] == "error"
    assert "boom" in finished["kwargs"]["error"]


async def test_reclaim_stale_requeues_then_fails(mock_supabase):
    # One stale job under the attempt cap (→ requeued), one at the cap (→ errored).
    stale = [{"id": "a", "attempts": 0}, {"id": "b", "attempts": 5}]
    mock_supabase.table.return_value.execute = AsyncMock(
        side_effect=[_resp(stale), _resp([{}]), _resp([{}])]
    )
    n = await jobs.reclaim_stale(stale_seconds=1.0, max_attempts=2)
    assert n == 2
    # Two conditional updates issued (one requeue, one terminal fail).
    assert mock_supabase.table.return_value.update.call_count == 2
