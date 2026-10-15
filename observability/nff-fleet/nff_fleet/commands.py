import asyncio
import secrets
import time
from typing import Any

from nff_fleet import signing
from nff_fleet.broker import router
from nff_fleet.config import settings


async def send_command(
    device_id: str,
    payload: dict[str, Any],
    project_id: str,
    timeout: float | None = None,
    session_id: str = "unknown",
) -> dict[str, Any]:
    if timeout is None:
        timeout = settings.command_timeout

    nonce = secrets.token_hex(4)
    full_payload = {**payload, "nonce": nonce, "timestamp": int(time.time())}
    # Signed with the device's project key — a command for project A won't verify on a
    # project B device (DEVICE_OWNERSHIP_DESIGN.md §6).
    signed = signing.sign(full_payload, project_id)

    # Audit log before publish (imported lazily to avoid circular imports)
    from nff_fleet import audit
    await audit.log_command(
        device_id=device_id,
        action=payload.get("action", "unknown"),
        nonce=nonce,
        session_id=session_id,
        project_id=project_id,
    )

    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    router.register_pending(device_id, fut)

    import json
    topic = f"nff/{project_id}/devices/{device_id}/cmd"
    body = json.dumps(signed, separators=(",", ":"))
    # Register for reconnect-redelivery: a QoS-0 publish is dropped if the device is not connected
    # at this instant, and a crash-looping device (the L2 recovery case) is only briefly online.
    # The router republishes this on every heartbeat/crash until the future resolves, so the
    # command lands inside a live window instead of failing once and never retrying.
    router.register_redeliver(device_id, topic, body)
    try:
        await router.publish(topic, body)
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(f"Device {device_id} did not respond within {timeout}s")
    finally:
        router.unregister_pending(device_id)
        router.unregister_redeliver(device_id)
