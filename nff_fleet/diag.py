from typing import Any

from nff_fleet.commands import send_command


async def get_diag(device_id: str) -> dict[str, Any]:
    resp = await send_command(device_id, {"action": "diag"})
    return {
        "device_id": device_id,
        "heap_free": resp.get("heap_free"),
        "heap_min": resp.get("heap_min"),
        "tasks": resp.get("tasks", []),
        "raw": resp,
    }
