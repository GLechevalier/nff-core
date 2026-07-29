"""Forward device-reported OTA results (and mid-flight progress) to nff-ota.

A device publishes ``{"type":"ota_result","status":...,"version":...}`` to its
``nff/devices/{id}/response`` topic after it reboots onto new firmware. The router
(``broker/router.py``) recognises that payload type and calls
``forward_ota_result``, which POSTs it to nff-ota's ``/internal/ota-result`` endpoint
so nff-ota can finalize the per-device deployment job.

During the update itself a device may also publish throttled
``{"type":"ota_progress","stage":...,"progress":...}`` ticks; ``forward_ota_progress``
relays them to ``/internal/ota-progress`` the same way.

Fire-and-forget (mirrors ``db._fire``): scheduling never blocks the router loop, and
a forward failure is logged + swallowed rather than crashing the loop. nff-ota's
reaper is the backstop if a forward is lost.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from nff_fleet.config import settings

logger = logging.getLogger(__name__)

# Progress is lossy QoS-0 telemetry that devices already throttle (>=5% / >=2 s); this
# per-device floor is just a flood guard against a misbehaving/hostile client.
_PROGRESS_MIN_INTERVAL_S = 1.0
_last_progress_forward: dict[str, float] = {}


def _fire(coro) -> None:
    async def _run():
        try:
            await coro
        except Exception as exc:  # noqa: BLE001 — never let a forward error escape
            logger.warning("ota-result forward error: %s", exc)
    asyncio.create_task(_run())


async def _post_ota_result(
    device_id: str, status: str, version: str, reason: str | None = None
) -> None:
    url = settings.ota_url.rstrip("/") + "/internal/ota-result"
    body: dict[str, Any] = {"device_id": device_id, "status": status, "version": version}
    # A rollback may carry a reason; keep it optional so the payload stays minimal and
    # back-compatible with devices/firmware that don't report one.
    if reason:
        body["reason"] = reason
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=body, headers={"X-Fleet-Secret": settings.mcp_secret})
    if resp.status_code != 200:
        logger.warning("ota-result forward to %s got %s: %s", url, resp.status_code, resp.text)


def forward_ota_result(device_id: str, payload: dict[str, Any]) -> None:
    """Schedule a forward of a device's ota_result to nff-ota. Non-blocking."""
    if not settings.ota_url:
        logger.debug("ota_result from %s but OTA_URL unset — not forwarding", device_id)
        return
    status = payload.get("status")
    version = payload.get("version")
    reason = payload.get("reason")
    if not status or not version:
        logger.warning("ota_result from %s missing status/version: %s", device_id, payload)
        return
    _fire(_post_ota_result(device_id, status, version, reason if isinstance(reason, str) else None))


async def _post_ota_progress(device_id: str, body: dict[str, Any]) -> None:
    url = settings.ota_url.rstrip("/") + "/internal/ota-progress"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=body, headers={"X-Fleet-Secret": settings.mcp_secret})
    if resp.status_code != 200:
        logger.warning("ota-progress forward to %s got %s: %s", url, resp.status_code, resp.text)


def forward_ota_progress(device_id: str, payload: dict[str, Any]) -> None:
    """Schedule a forward of a device's ota_progress tick to nff-ota. Non-blocking.

    Drops (rather than queues) anything above the per-device rate floor — a lost
    tick is harmless, the next one carries the fresher value anyway.
    """
    if not settings.ota_url:
        return
    version = payload.get("version")
    stage = payload.get("stage")
    if not version or not stage:
        logger.debug("ota_progress from %s missing version/stage: %s", device_id, payload)
        return

    # Only downloading ticks are throttled: stage transitions (installing/rebooting)
    # fire once each, milliseconds after the last download tick, and dropping one
    # loses a lifecycle step the dashboard renders.
    if stage == "downloading":
        now = time.monotonic()
        last = _last_progress_forward.get(device_id, 0.0)
        if now - last < _PROGRESS_MIN_INTERVAL_S:
            return
        _last_progress_forward[device_id] = now

    body: dict[str, Any] = {"device_id": device_id, "version": version, "stage": stage}
    progress = payload.get("progress")
    if isinstance(progress, int):
        body["progress"] = progress
    attempt = payload.get("attempt")
    if isinstance(attempt, int):
        body["attempt"] = attempt
    _fire(_post_ota_progress(device_id, body))
