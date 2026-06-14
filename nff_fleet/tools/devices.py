import time
from typing import Any

from nff_fleet.commands import send_command
from nff_fleet.registry import registry


def _resolve_project(device_id: str) -> str | None:
    """The project a device belongs to, learned from its cert-scoped heartbeat topic.

    A command can only be signed once the device's project is known; a device that has
    never connected (so has no project in the registry) cannot be commanded by id alone.
    """
    return registry.get_project(device_id)


async def fleet_list_devices() -> list[dict[str, Any]]:
    """Return all known devices with their current status."""
    return [
        {
            "device_id": d.device_id,
            "project_id": d.project_id,
            "status": d.status,
            "last_seen": d.last_seen,
            "heartbeat": d.heartbeat_payload,
        }
        for d in registry.all_devices()
    ]


async def fleet_ping(device_id: str) -> dict[str, Any]:
    """Send a signed ping to a device and return round-trip latency."""
    project_id = _resolve_project(device_id)
    if not project_id:
        return {"device_id": device_id, "error": "unknown project (device has not connected)"}
    t0 = time.monotonic()
    try:
        resp = await send_command(device_id, {"action": "ping"}, project_id)
        rtt_ms = round((time.monotonic() - t0) * 1000, 2)
        return {"device_id": device_id, "rtt_ms": rtt_ms, "response": resp}
    except TimeoutError as exc:
        return {"device_id": device_id, "error": str(exc)}


async def fleet_reboot(device_id: str) -> dict[str, Any]:
    """Send a signed reboot command to a device."""
    project_id = _resolve_project(device_id)
    if not project_id:
        return {"device_id": device_id, "error": "unknown project (device has not connected)"}
    try:
        resp = await send_command(device_id, {"action": "reboot"}, project_id)
        return {"device_id": device_id, "status": "rebooting", "response": resp}
    except TimeoutError as exc:
        return {"device_id": device_id, "error": str(exc)}
