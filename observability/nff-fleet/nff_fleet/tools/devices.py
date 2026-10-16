import os
import time
from typing import Any

from nff_fleet import db
from nff_fleet.commands import send_command
from nff_fleet.registry import registry

# Test-only chaos: fail the first N fleet_ping calls with a transient, retryable error so
# the agent-worker recovery loop (#6) can be exercised end-to-end (e2e Track 3). Gated by
# NFF_FLEET_CHAOS_FAIL_FIRST (unset/0 = off). A *raised* exception becomes an MCP is_error
# tool result — the error event the recovery loop classifies/retries on. NEVER set in prod.
_chaos_ping_calls = 0


def _chaos_fail_first() -> int:
    try:
        return int(os.environ.get("NFF_FLEET_CHAOS_FAIL_FIRST", "0") or "0")
    except ValueError:
        return 0


def _maybe_chaos_fail() -> None:
    global _chaos_ping_calls
    fail_first = _chaos_fail_first()
    if fail_first > 0 and _chaos_ping_calls < fail_first:
        _chaos_ping_calls += 1
        raise RuntimeError(
            "rate limited (chaos injection): 429 Too Many Requests — retry shortly"
        )


def reset_chaos() -> int:
    """Test-only: zero the per-process chaos counter so the next ``NFF_FLEET_CHAOS_FAIL_FIRST``
    pings fail again — lets the e2e transient track re-arm between iterations without a fleet
    restart. Does NOT arm chaos (the env stays the only switch), so it's a harmless no-op when
    chaos is off; returns the configured budget so the caller can confirm it's armed.
    """
    global _chaos_ping_calls
    _chaos_ping_calls = 0
    return _chaos_fail_first()


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
    _maybe_chaos_fail()
    project_id = _resolve_project(device_id)
    if not project_id:
        return {"device_id": device_id, "error": "unknown project (device has not connected)"}
    # A device broken in the self-heal demo (durable fault armed — migration 0080) is
    # unreachable: refuse the ping instead of round-tripping to the still-healthy mock, so the
    # reported state matches the dashboard's red "broken" dot. Best-effort (see is_device_broken).
    if await db.is_device_broken(device_id):
        return {"device_id": device_id, "error": "device unreachable — firmware crashed"}
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
