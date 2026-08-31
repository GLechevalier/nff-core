"""Idempotent seed-to-target on POST /mocks {target}.

`target` asks the controller to bring a project's per-level fleet up to N, computing the
deficit itself under a per-project lock. These prove it spawns only the deficit, no-ops when
already satisfied, and — the point of the lock — that two concurrent seeds converge on the
target instead of each spawning the full amount (the over-provisioning race). Docker + fleet
planes are stubbed, so nothing real is spawned.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from nff_mock_controller import docker as dk
from nff_mock_controller import provision, server
from nff_mock_controller.config import settings


@pytest.fixture(autouse=True)
def _stub_planes(monkeypatch):
    """Stub fleet + Docker. fake_spawn yields (await sleep 0) between reading and reflecting
    live state, so two concurrent requests genuinely interleave — without the per-project lock
    they would both observe the pre-spawn count and over-provision."""
    live: list[dk.MockInfo] = []

    async def fake_provision(device_id, project_id):
        return None

    async def fake_deprovision(device_id):
        return None

    async def fake_list_mocks():
        await asyncio.sleep(0)  # a real yield point at the read, to expose any race
        return list(live)

    async def fake_spawn(device_id, project_id, expires_at, *, profile,
                         buggy_versions=None, fw_version=None, display_name=None):
        await asyncio.sleep(0)  # widen the window between list and the reflected spawn
        live.append(dk.MockInfo(name=dk.container_name(device_id), state="running",
                                device_id=device_id, project_id=project_id,
                                expires_at=expires_at, level=str(profile.level)))
        return "deadbeefcafe0000"

    monkeypatch.setattr(provision, "provision", fake_provision)
    monkeypatch.setattr(provision, "deprovision", fake_deprovision)
    monkeypatch.setattr(dk, "list_mocks", fake_list_mocks)
    monkeypatch.setattr(dk, "spawn", fake_spawn)
    monkeypatch.setattr(settings, "control_secret", "test-secret")
    monkeypatch.setattr(settings, "max_level", 2)
    monkeypatch.setattr(settings, "default_level", 1)
    return {"live": live}


_AUTH = {"X-Control-Secret": "test-secret"}


async def _post(client, body):
    return await client.post("/mocks", json=body, headers=_AUTH)


async def _client():
    transport = httpx.ASGITransport(app=server.app)
    return httpx.AsyncClient(transport=transport, base_url="http://ctl")


async def test_target_spawns_the_deficit_from_empty():
    async with await _client() as client:
        resp = await _post(client, {"project_id": "p1", "level": 1, "target": 8})
    assert resp.status_code == 201
    assert len(resp.json()["spawned"]) == 8


async def test_target_is_idempotent_when_already_satisfied(_stub_planes):
    seed = {"project_id": "p1", "level": 1, "target": 8}
    async with await _client() as client:
        assert (await _post(client, seed)).status_code == 201
        resp = await _post(client, seed)
    assert resp.status_code == 200
    assert resp.json() == {"spawned": []}
    assert len(_stub_planes["live"]) == 8  # not 16


async def test_target_tops_up_only_the_remaining_deficit(_stub_planes):
    async with await _client() as client:
        await _post(client, {"project_id": "p1", "level": 1, "target": 5})
        resp = await _post(client, {"project_id": "p1", "level": 1, "target": 8})
    assert resp.status_code == 201
    assert len(resp.json()["spawned"]) == 3
    assert len(_stub_planes["live"]) == 8


async def test_target_is_per_level(_stub_planes):
    async with await _client() as client:
        await _post(client, {"project_id": "p1", "level": 1, "target": 8})
        resp = await _post(client, {"project_id": "p1", "level": 2, "target": 2})
    assert resp.status_code == 201
    assert len(resp.json()["spawned"]) == 2  # L1 count doesn't satisfy the L2 target
    l1 = [m for m in _stub_planes["live"] if m.level == "1"]
    l2 = [m for m in _stub_planes["live"] if m.level == "2"]
    assert len(l1) == 8 and len(l2) == 2


async def test_concurrent_seeds_converge_on_target(_stub_planes):
    """The race that produced >8 L1s: two seed-to-8 requests fired together must yield 8, not 16.
    fake_spawn yields, so without the per-project lock both would read 0 live and each spawn 8."""
    async with await _client() as client:
        r1, r2 = await asyncio.gather(
            _post(client, {"project_id": "p1", "level": 1, "target": 8}),
            _post(client, {"project_id": "p1", "level": 1, "target": 8}),
        )
    assert {r1.status_code, r2.status_code} <= {200, 201}
    total_spawned = len(r1.json()["spawned"]) + len(r2.json()["spawned"])
    assert total_spawned == 8
    assert len(_stub_planes["live"]) == 8  # exactly the target — the lock prevented over-spawn


async def test_concurrent_seeds_on_different_projects_dont_block(_stub_planes):
    """Per-project locking: two projects seed in parallel, each reaching its own target."""
    async with await _client() as client:
        r1, r2 = await asyncio.gather(
            _post(client, {"project_id": "pa", "level": 1, "target": 8}),
            _post(client, {"project_id": "pb", "level": 1, "target": 8}),
        )
    assert r1.status_code == 201 and r2.status_code == 201
    pa = [m for m in _stub_planes["live"] if m.project_id == "pa"]
    pb = [m for m in _stub_planes["live"] if m.project_id == "pb"]
    assert len(pa) == 8 and len(pb) == 8


async def test_negative_target_rejected():
    async with await _client() as client:
        resp = await _post(client, {"project_id": "p1", "level": 1, "target": -1})
    assert resp.status_code == 400
    assert "target must be >= 0" in resp.json()["error"]


async def test_non_integer_target_rejected():
    async with await _client() as client:
        resp = await _post(client, {"project_id": "p1", "level": 1, "target": "eight"})
    assert resp.status_code == 400
    assert "target must be an integer" in resp.json()["error"]
