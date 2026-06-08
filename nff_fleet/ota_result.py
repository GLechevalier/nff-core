"""Forward device-reported OTA results to nff-ota.

A device publishes ``{"type":"ota_result","status":...,"version":...}`` to its
``nff/devices/{id}/response`` topic after it reboots onto new firmware. The router
(``broker/router.py``) recognises that payload type and calls
``forward_ota_result``, which POSTs it to nff-ota's ``/internal/ota-result`` endpoint
so nff-ota can finalize the per-device deployment job.

Fire-and-forget (mirrors ``db._fire``): scheduling never blocks the router loop, and
a forward failure is logged + swallowed rather than crashing the loop. nff-ota's
reaper is the backstop if a forward is lost.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from nff_fleet.config import settings

logger = logging.getLogger(__name__)


def _fire(coro) -> None:
    async def _run():
        try:
            await coro
        except Exception as exc:  # noqa: BLE001 — never let a forward error escape
            logger.warning("ota-result forward error: %s", exc)
    asyncio.create_task(_run())


async def _post_ota_result(device_id: str, status: str, version: str) -> None:
    url = settings.ota_url.rstrip("/") + "/internal/ota-result"
    body = {"device_id": device_id, "status": status, "version": version}
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
    if not status or not version:
        logger.warning("ota_result from %s missing status/version: %s", device_id, payload)
        return
    _fire(_post_ota_result(device_id, status, version))
