"""Level routing in the controller — POST /mocks {level}.

These exercise the server's dispatch logic with the Docker + fleet planes stubbed: no real
containers are spawned. They prove level 0 spawns nothing, level 3 is a documented 501,
level 2 uses the L2 image + caps + label, and a level-less request still spawns L1.
"""

from __future__ import annotations

import httpx
import pytest

from nff_mock_controller import docker as dk
from nff_mock_controller import provision, server
from nff_mock_controller.config import settings


@pytest.fixture(autouse=True)
def _stub_planes(monkeypatch):
    """Stub the fleet provisioning + Docker driver so nothing real is touched, and record
    every spawn so a test can assert on the args the server chose per level."""
    spawns: list[dict] = []
    live: list[dk.MockInfo] = []

    async def fake_provision(device_id, project_id):
        return None

    async def fake_deprovision(device_id):
        return None

    async def fake_list_mocks():
        return list(live)

    async def fake_spawn(device_id, project_id, expires_at, *, profile,
                         buggy_versions=None, fw_version=None, display_name=None):
        spawns.append({"device_id": device_id, "project_id": project_id,
                       "profile": profile, "fw_version": fw_version})
        # Reflect the spawn into "live" state so per-level caps see it on the next call.
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
    return {"spawns": spawns, "live": live}


_AUTH = {"X-Control-Secret": "test-secret"}


async def _post(body):
    """One request against the ASGI app via a fresh, self-closing client."""
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ctl") as client:
        return await client.post("/mocks", json=body, headers=_AUTH)


async def test_no_level_defaults_to_l1(_stub_planes):
    resp = await _post({"project_id": "p1"})
    assert resp.status_code == 201
    spawned = resp.json()["spawned"]
    assert len(spawned) == 1 and spawned[0]["level"] == "1"
    prof = _stub_planes["spawns"][0]["profile"]
    assert prof.level == 1 and prof.image == settings.mock_image and prof.read_only is True


async def test_level_0_spawns_nothing(_stub_planes):
    resp = await _post({"project_id": "p1", "level": 0})
    assert resp.status_code == 200
    assert resp.json() == {"spawned": []}
    assert _stub_planes["spawns"] == []


async def test_level_3_not_implemented():
    resp = await _post({"project_id": "p1", "level": 3})
    assert resp.status_code == 501
    assert "not implemented" in resp.json()["error"]


async def test_level_2_uses_l2_image_and_label(_stub_planes):
    resp = await _post({"project_id": "p1", "level": 2})
    assert resp.status_code == 201
    assert resp.json()["spawned"][0]["level"] == "2"
    prof = _stub_planes["spawns"][0]["profile"]
    assert prof.level == 2
    assert prof.image == settings.l2_image
    assert prof.read_only is settings.l2_read_only
    assert prof.device_type == settings.l2_device_type


async def test_tier_alias_accepted(_stub_planes):
    resp = await _post({"project_id": "p1", "tier": 2})
    assert resp.status_code == 201
    assert _stub_planes["spawns"][0]["profile"].level == 2


async def test_level_above_max_rejected(monkeypatch):
    monkeypatch.setattr(settings, "max_level", 1)
    resp = await _post({"project_id": "p1", "level": 2})
    assert resp.status_code == 400
    assert "between 0 and 1" in resp.json()["error"]


async def test_l2_per_project_cap(_stub_planes, monkeypatch):
    monkeypatch.setattr(settings, "max_l2_per_project", 2)
    monkeypatch.setattr(settings, "max_l2_total", 10)
    # Two L2 mocks are allowed; the third trips the per-project cap.
    for _ in range(2):
        assert (await _post({"project_id": "p1", "level": 2})).status_code == 201
    resp = await _post({"project_id": "p1", "level": 2})
    assert resp.status_code == 429
    assert "per-project cap reached for level 2" in resp.json()["error"]


async def test_l2_caps_independent_of_l1(_stub_planes, monkeypatch):
    """An L2 mock must not count against the (much larger) L1 caps, and vice-versa."""
    monkeypatch.setattr(settings, "max_l2_total", 1)
    assert (await _post({"project_id": "p1", "level": 2})).status_code == 201
    # L2 is now full, but an L1 spawn is unaffected.
    resp = await _post({"project_id": "p1", "level": 1})
    assert resp.status_code == 201
    # A second L2 trips its own total cap.
    resp = await _post({"project_id": "p2", "level": 2})
    assert resp.status_code == 429
