from typing import Any

from nff_fleet import diag as diag_module


async def fleet_get_diag(device_id: str) -> dict[str, Any]:
    """Send a diagnostics command to a device and return heap stats + FreeRTOS task list."""
    try:
        return await diag_module.get_diag(device_id)
    except TimeoutError as exc:
        return {"device_id": device_id, "error": str(exc)}
