"""fleet_ping refuses a device broken in the self-heal demo (durable fault armed).

nff-fleet has no pytest-asyncio config, so async code is driven via asyncio.run().
"""
import asyncio

import nff_fleet.tools.devices as devices


def test_fleet_ping_refuses_broken_device(monkeypatch):
    """A broken device is unreachable: fleet_ping returns the error and never round-trips
    to the (still-healthy) mock via send_command."""
    monkeypatch.setattr(devices, "_resolve_project", lambda d: "proj-1")

    async def _broken(_device_id):
        return True
    monkeypatch.setattr(devices.db, "is_device_broken", _broken)

    async def _boom(*_a, **_k):
        raise AssertionError("send_command must not be called for a broken device")
    monkeypatch.setattr(devices, "send_command", _boom)

    out = asyncio.run(devices.fleet_ping("mock-1"))
    assert "unreachable" in out["error"]
    assert "rtt_ms" not in out


def test_fleet_ping_pings_healthy_device(monkeypatch):
    """A healthy device still pings normally (the gate only fires when broken)."""
    monkeypatch.setattr(devices, "_resolve_project", lambda d: "proj-1")

    async def _healthy(_device_id):
        return False
    monkeypatch.setattr(devices.db, "is_device_broken", _healthy)

    async def _send(device_id, _payload, _project_id):
        return {"type": "pong", "id": device_id}
    monkeypatch.setattr(devices, "send_command", _send)

    out = asyncio.run(devices.fleet_ping("mock-1"))
    assert out["response"]["type"] == "pong"
    assert "rtt_ms" in out
