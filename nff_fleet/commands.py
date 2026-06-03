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
    timeout: float | None = None,
    session_id: str = "unknown",
) -> dict[str, Any]:
    if timeout is None:
        timeout = settings.command_timeout

    nonce = secrets.token_hex(4)
    full_payload = {**payload, "nonce": nonce, "timestamp": int(time.time())}
    signed = signing.sign(full_payload)

    # Audit log before publish (imported lazily to avoid circular imports)
    from nff_fleet import audit
    await audit.log_command(
        device_id=device_id,
        action=payload.get("action", "unknown"),
        nonce=nonce,
        session_id=session_id,
    )

    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    router.register_pending(nonce, fut)

    import json
    topic = f"nff/devices/{device_id}/cmd"
    try:
        await router.publish(topic, json.dumps(signed))
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(f"Device {device_id} did not respond within {timeout}s")
    finally:
        router.unregister_pending(nonce)
