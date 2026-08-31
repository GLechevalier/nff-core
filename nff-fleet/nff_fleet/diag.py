from typing import Any

from nff_fleet.commands import send_command
from nff_fleet.registry import registry


async def get_diag(device_id: str) -> dict[str, Any]:
    project_id = registry.get_project(device_id)
    if not project_id:
        return {"device_id": device_id, "error": "unknown project (device has not connected)"}
    resp = await send_command(device_id, {"action": "diag"}, project_id)
    return {
        "device_id": device_id,
        "heap_free": resp.get("heap_free"),
        "heap_min": resp.get("heap_min"),
        "tasks": resp.get("tasks", []),
        "raw": resp,
    }
