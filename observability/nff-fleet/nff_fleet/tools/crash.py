from typing import Any

from nff_fleet import crash as crash_module


async def fleet_get_crash(device_id: str) -> dict[str, Any]:
    """Return the most recent crash report for a device (works even when offline)."""
    report = crash_module.get_crash(device_id)
    if report is None:
        return {"device_id": device_id, "crash": None, "message": "No crash report cached"}
    return {"device_id": device_id, "crash": report}


async def fleet_resolve_backtrace(device_id: str, build_id: str) -> dict[str, Any]:
    """
    Resolve the backtrace from the cached crash report for a device using addr2line.

    Requires the ELF to have been stored via fleet_store_elf first.
    """
    report = crash_module.get_crash(device_id)
    if report is None:
        return {"device_id": device_id, "error": "No crash report cached for this device"}

    addrs = report.get("backtrace", [])
    if not addrs:
        return {"device_id": device_id, "error": "Crash report has no backtrace addresses"}

    if isinstance(addrs, str):
        addrs = addrs.split()

    resolved = await crash_module.resolve_backtrace(build_id, addrs)
    return {"device_id": device_id, "build_id": build_id, "resolved": resolved}
