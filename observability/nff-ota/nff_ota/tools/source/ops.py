"""MCP-facing tools for the virtual firmware store (the agent's read/fix surface).

Thin wrappers over ``nff_ota.source.ops`` so the agent's tool descriptions live here while
the store + build + deploy logic stays in one place (shared with the harness seed route).
"""

from __future__ import annotations

from nff_ota.models.source import FirmwareFixJob, GetFirmwareSourceResult
from nff_ota.source import ops as _core


async def get_firmware_source(
    device_id: str, include_source: bool = False
) -> GetFirmwareSourceResult:
    """Read the fictional firmware a mock device is currently running.

    Compact by default: returns the source id (``build_id``), ``version``, ``lines`` (the source's
    line count), ``author``, and the ``fault`` the build found ({"scenario": ...} or null) — but
    NOT the source text. Use this first when a device is crash-looping to identify the bug class.

    Pass ``include_source=True`` to also get the full source (a pseudo-`.ino`) in the ``source``
    field — do that when you are about to edit it to ship a fix.
    """
    return await _core.get_firmware_source(device_id, include_source=include_source)


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
    if the version/target is invalid (nothing was enqueued in that case). You MUST then call
    ``get_firmware_fix_status(job_id=..., wait_seconds=60)`` repeatedly — it blocks server-side,
    so never sleep or set a timer yourself — until ``status`` becomes ``done`` or
    ``error``. On ``done``, check ``fault_remaining``: if it is true the build still found the
    bug in your source (the fix did not work and the device will keep crash-looping), so edit
    the source and submit again with a higher version.
    """
    return await _core.submit_firmware_fix(device_id, version, source, changelog)


async def get_firmware_fix_status(job_id: str, wait_seconds: int = 0) -> FirmwareFixJob:
    """Poll the status of a firmware fix started with ``submit_firmware_fix``.

    Returns ``{"job_id", "status", ...}`` where ``status`` is one of ``pending`` (queued),
    ``running`` (compiling+deploying now), ``done`` (rolled out — check ``fault_remaining``),
    or ``error`` (``error`` explains why).

    Pass ``wait_seconds=60`` to block SERVER-SIDE until the job finishes or the wait elapses
    (max 90), then call again while ``status`` is ``pending`` or ``running``. Never sleep or
    set a timer client-side — this tool is the wait. A real firmware build takes minutes, so
    do not give up early. On ``done`` with ``fault_remaining`` false the fix shipped and the
    device should recover on its next OTA cycle.
    """
    return await _core.get_firmware_fix_status(job_id, wait_seconds=wait_seconds)
