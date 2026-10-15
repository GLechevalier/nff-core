from typing import Any

from nff_fleet import db


async def fleet_get_serial_log(
    device_id: str,
    limit: int = 100,
    since: str | None = None,
    grep: str | None = None,
) -> dict[str, Any]:
    """Read a device's serial console history, captured while the device was on
    the wired USB link (the dashboard uploads its Serial Monitor to the cloud).

    USE THIS FIRST when diagnosing OTA rollbacks, boot loops, TLS/certificate
    failures, or any freshly flashed device misbehaving — the root cause (e.g.
    an mbedtls error code, a rollback reason, a missing-CA warning) is usually
    printed right here and nowhere else.

    `since`: ISO timestamp lower bound (e.g. "2026-08-13T20:00:00Z").
    `grep`: case-insensitive substring filter (e.g. "mbedtls", "rollback").
    Lines return oldest-first (newest last); `limit` is clamped to 1..500.
    """
    limit = max(1, min(limit, 500))
    lines = await db.get_serial_lines(device_id, limit=limit, since=since, grep=grep)
    if not lines:
        return {
            "device_id": device_id,
            "lines": [],
            "count": 0,
            "message": (
                "No serial log rows for this query. Console lines exist only for "
                "periods when a dashboard tab held the wired USB link to this device "
                "(and are pruned after the retention window)."
            ),
        }
    return {
        "device_id": device_id,
        "count": len(lines),
        "truncated": len(lines) == limit,
        "lines": lines,
    }
