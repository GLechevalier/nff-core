import asyncio
import logging

from nff_fleet.config import settings
from nff_fleet.registry import registry
from nff_fleet import db

logger = logging.getLogger(__name__)

_reaper_task: asyncio.Task | None = None


async def _reaper_loop() -> None:
    try:
        while True:
            await asyncio.sleep(settings.reaper_interval)
            for d in registry.stale_online_devices(settings.heartbeat_timeout):
                registry.set_offline(d.device_id)
                db.upsert_device(d.device_id, {"status": "offline"})
                logger.info(
                    "Device %s marked offline (no heartbeat for >%ss)",
                    d.device_id,
                    settings.heartbeat_timeout,
                )
    except asyncio.CancelledError:
        pass


async def start_reaper() -> None:
    global _reaper_task
    _reaper_task = asyncio.create_task(_reaper_loop())
    logger.info(
        "Reaper started (timeout=%ss, interval=%ss)",
        settings.heartbeat_timeout,
        settings.reaper_interval,
    )


async def stop_reaper() -> None:
    global _reaper_task
    if _reaper_task is not None:
        _reaper_task.cancel()
        try:
            await _reaper_task
        except asyncio.CancelledError:
            pass
        _reaper_task = None
    logger.info("Reaper stopped")
