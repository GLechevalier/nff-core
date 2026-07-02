"""MCP-facing tools for the virtual firmware store (the agent's read/fix surface).

Thin wrappers over ``nff_ota.source.ops`` so the agent's tool descriptions live here while
the store + build + deploy logic stays in one place (shared with the harness seed route).
"""

from __future__ import annotations

from nff_ota.models.source import GetFirmwareSourceResult, SubmitFirmwareFixResult
from nff_ota.source import ops as _core


async def get_firmware_source(device_id: str) -> GetFirmwareSourceResult:
    """Read the fictional firmware source a mock device is currently running.

    Returns the source (a pseudo-`.ino`), its semver, build id, author, and the ``fault`` the
    build found in it ({"scenario": ...} or null). Use this first when a device is
    crash-looping: the bug is in this source, and fixing it is what cures the device.
    """
    return await _core.get_firmware_source(device_id)


async def submit_firmware_fix(
    device_id: str, version: str, source: str, changelog: str | None = None
) -> SubmitFirmwareFixResult:
    """Ship a fixed firmware source as the next release and roll it out to the device.

    Pass the FULL fixed source (not a diff) and a ``version`` that is a bare semver
    (major.minor.patch) strictly greater than the version the device runs now — the device's
    anti-downgrade gate rejects anything not strictly higher. This records a new revision,
    compiles the source, and deploys it over OTA. The device adopts the new code and recovers
    if the bug is gone. The result's ``fault_remaining`` is true when the build still finds a
    bug in your source — meaning the fix did not work and the device will keep crash-looping.
    """
    return await _core.submit_firmware_fix(device_id, version, source, changelog)
