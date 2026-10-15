"""Integration tests for the end-of-deployment agent hand-off.

Uses a PER-TABLE Supabase mock (unlike conftest's single shared table mock) because the
notify flow reads five different tables and the assertions are per-table.
"""
from __future__ import annotations

from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

import nff_ota.agent_notify as agent_notify
import nff_ota.client as client_module
from nff_ota.config import settings
from nff_ota.tools.deployments import ops

_CHAIN = (
    "select", "insert", "update", "delete", "upsert",
    "eq", "neq", "in_", "lt", "gt", "gte", "lte", "order", "limit",
)


def make_pertable_client(responses: dict[str, list[list[dict]]]):
    """A Supabase client mock where each table has its own mock and an ordered queue of
    .execute() payloads (empty data once the queue is exhausted)."""
    tables: dict[str, MagicMock] = {}
    queues = {name: deque(seq) for name, seq in responses.items()}

    def table(name: str) -> MagicMock:
        if name not in tables:
            tm = MagicMock(name=f"table:{name}")
            for m in _CHAIN:
                getattr(tm, m).return_value = tm

            async def execute(_name=name):
                q = queues.get(_name)
                data = q.popleft() if q else []
                return MagicMock(data=data, error=None)

            tm.execute = AsyncMock(side_effect=execute)
            tables[name] = tm
        return tables[name]

    client = MagicMock()
    client.table.side_effect = table
    return client, tables


def _dep_row(status: str = "active", **over) -> dict:
    row = {
        "id": "dep-1",
        "name": "rollout-blink",
        "target_version": "1.2.3",
        "device_group_id": "group-1",
        "status": status,
        "project_id": "proj-1",
        "max_devices_in_flight": 10,
        "retries": 3,
        "created_at": "2026-07-30T10:00:00Z",
        "start_time": None,
        "phases": None,
        "current_phase": None,
        "halted_reason": None,
    }
    row.update(over)
    return row


def _notify_tables(
    *,
    dep_status: str = "finished",
    jobs: list[dict] | None = None,
    project: dict | None = None,
    pipeline_runs: list[dict] | None = None,
    connections: list[list[dict]] | None = None,
    repeats: int = 1,
) -> dict[str, list[list[dict]]]:
    """Table responses for `repeats` consecutive notify_deployment_terminal calls."""
    jobs = jobs if jobs is not None else [
        {"device_id": "dev-a", "status": "success", "failure_reason": None, "wave": 0}
    ]
    project = project or {"is_demo": False, "is_pool": False}
    return {
        "deployments": [[_dep_row(dep_status)]] * repeats,
        "projects": [[project]] * repeats,
        "ota_jobs": [jobs] * repeats,
        "pipeline_runs": [pipeline_runs or []] * repeats,
        "github_connections": (connections or [[]]) * repeats,
        "agent_messages": [[{"id": "m"}]] * repeats,
    }


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(settings, "agent_on_finish", True)
    monkeypatch.setattr(settings, "agent_on_finish_skip_demo", True)
    monkeypatch.setattr(settings, "agent_on_finish_cooldown_seconds", 900.0)
    agent_notify._last_emit.clear()
    yield
    agent_notify._last_emit.clear()


def _install(client) -> None:
    client_module._client = client


@pytest.fixture(autouse=True)
def _restore_client():
    original = client_module._client
    yield
    client_module._client = original


def _inserted_row(tables) -> dict:
    assert tables["agent_messages"].insert.call_count == 1
    return tables["agent_messages"].insert.call_args[0][0]


def _assert_no_handoff(tables) -> None:
    # Table mocks are created lazily on first client.table(name) — a table the code never
    # touched is itself proof the insert didn't happen.
    assert "agent_messages" not in tables or tables["agent_messages"].insert.call_count == 0


# -- roll-up (CAS) path ----------------------------------------------------------------


async def test_recompute_emits_once_on_cas_win():
    responses = _notify_tables()
    # _recompute_deployment_status's own reads come FIRST on the same tables:
    responses["deployments"] = (
        [[_dep_row("active")], [_dep_row("finished")]] + responses["deployments"]
    )
    responses["ota_jobs"] = [[{"status": "success"}]] + responses["ota_jobs"]
    client, tables = make_pertable_client(responses)
    _install(client)

    await ops._recompute_deployment_status("dep-1")

    row = _inserted_row(tables)
    assert row["role"] == "user"
    assert row["project_id"] == "proj-1"
    assert row["id"]
    assert "rollout-blink" in row["content"]
    tables["deployments"].update.assert_called_once_with({"status": "finished"})


async def test_recompute_cas_lost_no_emit():
    client, tables = make_pertable_client({
        "deployments": [[_dep_row("active")], []],  # select, then CAS update returns no rows
        "ota_jobs": [[{"status": "success"}]],
    })
    _install(client)

    await ops._recompute_deployment_status("dep-1")

    _assert_no_handoff(tables)


@pytest.mark.parametrize("status", ["finished", "aborted", "paused"])
async def test_recompute_terminal_or_paused_no_emit(status):
    client, tables = make_pertable_client({"deployments": [[_dep_row(status)]]})
    _install(client)

    await ops._recompute_deployment_status("dep-1")

    tables["deployments"].update.assert_not_called()
    _assert_no_handoff(tables)


async def test_recompute_jobs_not_all_terminal_no_emit():
    client, tables = make_pertable_client({
        "deployments": [[_dep_row("active")]],
        "ota_jobs": [[{"status": "success"}, {"status": "downloading"}]],
    })
    _install(client)

    await ops._recompute_deployment_status("dep-1")

    tables["deployments"].update.assert_not_called()
    _assert_no_handoff(tables)


# -- abort path --------------------------------------------------------------------------


async def test_abort_emits_aborted_summary():
    responses = _notify_tables(dep_status="active")
    # abort_deployment consumes deployments select + update, and an ota_jobs bulk update,
    # BEFORE notify's own reads on the same tables:
    responses["deployments"] = (
        [[_dep_row("active")], [_dep_row("aborted")]] + responses["deployments"]
    )
    responses["ota_jobs"] = [[]] + responses["ota_jobs"]
    client, tables = make_pertable_client(responses)
    _install(client)

    result = await ops.abort_deployment("dep-1")

    assert result.error is None
    row = _inserted_row(tables)
    assert "Final status: aborted" in row["content"]


# -- notify gates ------------------------------------------------------------------------


async def test_master_switch_off_no_reads_no_emit(monkeypatch):
    monkeypatch.setattr(settings, "agent_on_finish", False)
    client, tables = make_pertable_client(_notify_tables())
    _install(client)

    await agent_notify.notify_deployment_terminal("dep-1", "finished")

    client.table.assert_not_called()


@pytest.mark.parametrize(
    "project", [{"is_demo": True, "is_pool": False}, {"is_demo": False, "is_pool": True}]
)
async def test_demo_and_pool_projects_skipped(project):
    client, tables = make_pertable_client(_notify_tables(project=project))
    _install(client)

    await agent_notify.notify_deployment_terminal("dep-1", "finished")

    _assert_no_handoff(tables)


async def test_cooldown_suppresses_second_success_but_not_failure():
    ok_jobs = [{"device_id": "dev-a", "status": "success", "failure_reason": None, "wave": 0}]
    bad_jobs = [
        {"device_id": "dev-b", "status": "failure", "failure_reason": "rollback", "wave": 0}
    ]
    responses = _notify_tables(repeats=3)
    responses["ota_jobs"] = [ok_jobs, ok_jobs, bad_jobs]
    client, tables = make_pertable_client(responses)
    _install(client)

    await agent_notify.notify_deployment_terminal("dep-1", "finished")  # emits, stamps cooldown
    await agent_notify.notify_deployment_terminal("dep-1", "finished")  # all-success â†’ suppressed
    assert tables["agent_messages"].insert.call_count == 1

    await agent_notify.notify_deployment_terminal("dep-1", "finished")  # failure â†’ bypasses
    assert tables["agent_messages"].insert.call_count == 2


async def test_pipeline_managed_failure_summary_only():
    jobs = [{"device_id": "dev-a", "status": "failure", "failure_reason": "rollback", "wave": 0}]
    client, tables = make_pertable_client(_notify_tables(
        jobs=jobs,
        pipeline_runs=[{"id": "r-1", "commit_sha": "abcdef1234567", "connection_id": "c-1"}],
        connections=[[{"repo_full_name": "acme/fw"}]],
    ))
    _install(client)

    await agent_notify.notify_deployment_terminal("dep-1", "finished")

    content = _inserted_row(tables)["content"]
    assert "repo acme/fw@abcdef1" in content
    assert "do NOT open a PR" in content
    assert "open_fix_pr(" not in content
    assert "submit_firmware_fix(" not in content


async def test_non_pipeline_failure_gets_repo_playbook():
    jobs = [{"device_id": "dev-a", "status": "failure", "failure_reason": "rollback", "wave": 0}]
    client, tables = make_pertable_client(_notify_tables(
        jobs=jobs, connections=[[{"repo_full_name": "acme/fw"}]],
    ))
    _install(client)

    await agent_notify.notify_deployment_terminal("dep-1", "finished")

    content = _inserted_row(tables)["content"]
    assert "open_fix_pr(repo_full_name='acme/fw'" in content


async def test_non_pipeline_failure_no_repo_gets_local_playbook():
    jobs = [{"device_id": "dev-a", "status": "timed_out", "failure_reason": None, "wave": 0}]
    client, tables = make_pertable_client(_notify_tables(jobs=jobs))
    _install(client)

    await agent_notify.notify_deployment_terminal("dep-1", "finished")

    content = _inserted_row(tables)["content"]
    assert "submit_firmware_fix(" in content
    assert "get_firmware_fix_status(" in content
    assert "open_fix_pr" not in content


async def test_insert_failure_never_raises():
    client, tables = make_pertable_client(_notify_tables())
    tables_holder = tables
    # Force the agent_messages insert to blow up.
    client.table("agent_messages").execute = AsyncMock(side_effect=RuntimeError("boom"))
    _install(client)

    await agent_notify.notify_deployment_terminal("dep-1", "finished")  # must not raise

    tables_holder["agent_messages"].insert.assert_called_once()

