"""MCP-facing tools for the virtual firmware store (the agent's read/fix surface).

Thin wrappers over ``nff_ota.source.ops`` so the agent's tool descriptions live here while
the store + build + deploy logic stays in one place (shared with the harness seed route).
"""

from __future__ import annotations

from nff_ota.models.source import FirmwareFixJob, GetFirmwareSourceResult
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
) -> FirmwareFixJob:
    """Ship a fixed firmware source as the next release and roll it out to the device.

    Pass the FULL fixed source (not a diff) and a ``version`` that is a bare semver
    (major.minor.patch) strictly greater than the version the device runs now — the device's
    anti-downgrade gate rejects anything not strictly higher.

    This is ASYNCHRONOUS: compiling the firmware for real takes many minutes, so the tool
    does NOT wait for the build. It validates your request and returns a job handle right
    away — ``{"job_id": ..., "status": "pending"}`` — or ``{"status": "error", "error": ...}``
    if the version/target is invalid (nothing was enqueued in that case). You MUST then poll
    ``get_firmware_fix_status(job_id=...)`` every ~30-60s until ``status`` becomes ``done`` or
    ``error``. On ``done``, check ``fault_remaining``: if it is true the build still found the
    bug in your source (the fix did not work and the device will keep crash-looping), so edit
    the source and submit again with a higher version.
    """
    return await _core.submit_firmware_fix(device_id, version, source, changelog)


async def get_firmware_fix_status(job_id: str) -> FirmwareFixJob:
    """Poll the status of a firmware fix started with ``submit_firmware_fix``.

    Returns ``{"job_id", "status", ...}`` where ``status`` is one of ``pending`` (queued),
    ``running`` (compiling+deploying now), ``done`` (rolled out — check ``fault_remaining``),
    or ``error`` (``error`` explains why). Keep polling every ~30-60s while ``status`` is
    ``pending`` or ``running``; a real firmware build takes many minutes, so do not give up
    early. On ``done`` with ``fault_remaining`` false the fix shipped and the device should
    recover on its next OTA cycle.
    """
    return await _core.get_firmware_fix_status(job_id)
