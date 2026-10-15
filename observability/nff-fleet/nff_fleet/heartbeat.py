"""Service liveness heartbeat.

Announces this server's presence to the dashboard's ``service_status`` table on
a timer. The dashboard treats the service as offline once a beat goes stale, so
both a clean shutdown and a crash surface as 'offline' with no extra signaling.
"""

from __future__ import annotations

import asyncio
import logging

from nff_fleet import db

logger = logging.getLogger(__name__)

SERVICE_NAME = "nff-fleet"
# Seconds between beats. Keep comfortably below the dashboard's stale window
# (~30s) so a couple of dropped beats don't flip the service to offline.
HEARTBEAT_INTERVAL = 10.0

_task: asyncio.Task | None = None


async def _loop() -> None:
    # Beat immediately on the first iteration, then once per interval.
    while True:
        try:
            await db.service_heartbeat(SERVICE_NAME)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let a transient DB error kill the loop
            logger.warning("heartbeat error: %s", exc)
        await asyncio.sleep(HEARTBEAT_INTERVAL)


async def start_heartbeat() -> None:
    global _task
    _task = asyncio.create_task(_loop())
    logger.info(
        "Heartbeat started (service=%s, interval=%ss)", SERVICE_NAME, HEARTBEAT_INTERVAL
    )


async def stop_heartbeat() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
    logger.info("Heartbeat stopped")
