"""Keep-alive: browser presence protects L1 mocks; L2 mocks follow an idle lease.

These prove the reaper's keep-alive rules with the Docker + fleet planes stubbed (no real
containers), plus the POST /mocks/presence and /mocks/renew route contract.

    L1: never reaped while its project has a fresh presence ping; reaps once presence goes
        stale (user offline), falling back to its TTL label.
    L2: presence does NOT keep it alive; it reaps on an idle deadline, which /mocks/renew
        (employing it) pushes forward.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from nff_mock_controller import docker as dk
from nff_mock_controller import presence, provision, reaper, server
from nff_mock_controller.config import settings


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _past() -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(seconds=10))


def _future() -> str:
    return _iso(datetime.now(timezone.utc) + timedelta(hours=1))


@pytest.fixture(autouse=True)
def _clean_state():
    """Each test starts with empty keep-alive stores (they are module-level dicts)."""
    presence._presence.clear()
    presence._renew_until.clear()
    yield
    presence._presence.clear()
    presence._renew_until.clear()


@pytest.fixture
def _stub_docker(monkeypatch):
    """Stub the docker driver + fleet deprovision; record which mocks the reaper tears down."""
    live: list[dk.MockInfo] = []
    killed: list[str] = []

    async def fake_list_mocks():
        return list(live)

    async def fake_kill_and_rm(name):
        killed.append(name)

    async def fake_deprovision(device_id):
        return None

    monkeypatch.setattr(dk, "list_mocks", fake_list_mocks)
    monkeypatch.setattr(dk, "kill_and_rm", fake_kill_and_rm)
    monkeypatch.setattr(provision, "deprovision", fake_deprovision)
    return {"live": live, "killed": killed}


def _mock(device_id, project_id, level, expires_at, state="running"):
    return dk.MockInfo(name=dk.container_name(device_id), state=state,
                       device_id=device_id, project_id=project_id,
                       expires_at=expires_at, level=level)


# ── reaper: L1 presence ───────────────────────────────────────────────────────
async def test_expired_l1_reaped_without_presence(_stub_docker):
    _stub_docker["live"].append(_mock("mock-p1-a", "p1", "1", _past()))
    assert await reaper.sweep_once() == 1
    assert _stub_docker["killed"] == [dk.container_name("mock-p1-a")]


async def test_expired_l1_survives_with_fresh_presence(_stub_docker):
    _stub_docker["live"].append(_mock("mock-p1-a", "p1", "1", _past()))
    presence.mark_presence("p1")  # user is connected → never reap the L1
    assert await reaper.sweep_once() == 0
    assert _stub_docker["killed"] == []


async def test_l1_reaped_once_presence_goes_stale(_stub_docker, monkeypatch):
    monkeypatch.setattr(settings, "presence_ttl_s", 60)
    _stub_docker["live"].append(_mock("mock-p1-a", "p1", "1", _past()))
    # A ping older than presence_ttl_s = user has left.
    presence._presence["p1"] = datetime.now(timezone.utc) - timedelta(seconds=120)
    assert await reaper.sweep_once() == 1


async def test_presence_does_not_save_dead_l1(_stub_docker):
    """A crashed/exited container is torn down even while the user is online."""
    _stub_docker["live"].append(_mock("mock-p1-a", "p1", "1", _future(), state="exited"))
    presence.mark_presence("p1")
    assert await reaper.sweep_once() == 1


# ── reaper: L2 idle lease ─────────────────────────────────────────────────────
async def test_presence_does_not_protect_l2(_stub_docker):
    """L2 is scarce — presence must not keep an expired one alive."""
    _stub_docker["live"].append(_mock("mock-p1-l2", "p1", "2", _past()))
    presence.mark_presence("p1")
    assert await reaper.sweep_once() == 1


async def test_renew_keeps_l2_alive_past_label(_stub_docker):
    _stub_docker["live"].append(_mock("mock-p1-l2", "p1", "2", _past()))
    presence.renew_device("mock-p1-l2", 600)  # employed → idle deadline in the future
    assert await reaper.sweep_once() == 0
    assert _stub_docker["killed"] == []


async def test_l2_reaped_when_idle_deadline_passes(_stub_docker):
    _stub_docker["live"].append(_mock("mock-p1-l2", "p1", "2", _future()))
    # An idle deadline already in the past overrides the still-future label.
    presence._renew_until["mock-p1-l2"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert await reaper.sweep_once() == 1


async def test_prune_forgets_dead_device_renew(_stub_docker):
    presence._renew_until["mock-gone"] = datetime.now(timezone.utc) + timedelta(hours=1)
    _stub_docker["live"].append(_mock("mock-p1-l2", "p1", "2", _future()))
    presence.renew_device("mock-p1-l2", 600)
    await reaper.sweep_once()
    assert "mock-gone" not in presence._renew_until          # pruned (no longer live)
    assert "mock-p1-l2" in presence._renew_until             # still live → kept


# ── routes ────────────────────────────────────────────────────────────────────
_AUTH = {"X-Control-Secret": "test-secret"}


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(settings, "control_secret", "test-secret")


async def _post(path, body, headers=_AUTH):
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ctl") as client:
        return await client.post(path, json=body, headers=headers)


async def test_presence_route_marks_project():
    resp = await _post("/mocks/presence", {"project_id": "p1"})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert presence.presence_fresh("p1")


async def test_presence_route_requires_secret():
    resp = await _post("/mocks/presence", {"project_id": "p1"}, headers={})
    assert resp.status_code == 401
    assert not presence.presence_fresh("p1")


async def test_presence_route_requires_project():
    resp = await _post("/mocks/presence", {})
    assert resp.status_code == 400


async def test_renew_route_sets_deadline():
    resp = await _post("/mocks/renew", {"device_id": "mock-p1-l2", "ttl_s": 300})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert presence.renew_deadline("mock-p1-l2") is not None


async def test_renew_route_rejects_non_mock_id():
    resp = await _post("/mocks/renew", {"device_id": "test-device-01"})
    assert resp.status_code == 400
    assert presence.renew_deadline("test-device-01") is None


async def test_renew_route_defaults_ttl_to_l2_idle(monkeypatch):
    monkeypatch.setattr(settings, "l2_idle_ttl_s", 600)
    before = datetime.now(timezone.utc)
    resp = await _post("/mocks/renew", {"device_id": "mock-p1-l2"})
    assert resp.status_code == 200
    deadline = datetime.fromisoformat(resp.json()["expires_at"])
    # ~600 s out (allow slack for execution time).
    assert timedelta(seconds=590) <= deadline - before <= timedelta(seconds=610)
