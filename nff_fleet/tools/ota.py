import base64
from typing import Any

from nff_fleet import ota as ota_module
from nff_fleet.commands import send_command
from nff_fleet.config import settings


async def fleet_store_elf(build_id: str, elf_bytes_b64: str) -> dict[str, Any]:
    """Store a base64-encoded ELF binary under /data/firmware/{build_id}/firmware.elf."""
    data = base64.b64decode(elf_bytes_b64)
    dest = await ota_module.store_elf(build_id, data)
    return {"build_id": build_id, "path": str(dest), "size_bytes": len(data)}


async def fleet_push_ota(
    device_id: str,
    firmware_url: str,
    build_id: str,
    confirmed: bool = False,
) -> dict[str, Any]:
    """
    Push an OTA update to a device.

    If device_id is a comma-separated list of more than 10 devices, confirmed=True
    is required to proceed.
    """
    device_ids = [d.strip() for d in device_id.split(",") if d.strip()]
    if len(device_ids) > settings.ota_bulk_limit and not confirmed:
        return {
            "error": (
                f"Bulk OTA to {len(device_ids)} devices requires confirmed=True. "
                f"Limit is {settings.ota_bulk_limit}."
            )
        }

    payload = ota_module.ota_payload(build_id, firmware_url)
    results = []
    for dev in device_ids:
        try:
            resp = await send_command(dev, payload, timeout=120.0)
            results.append({"device_id": dev, "status": "ok", "response": resp})
        except TimeoutError as exc:
            results.append({"device_id": dev, "status": "timeout", "error": str(exc)})
        except Exception as exc:
            results.append({"device_id": dev, "status": "error", "error": str(exc)})

    return {"results": results}
