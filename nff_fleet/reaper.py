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
            for d in registry.stale_online_devices(
                settings.default_heartbeat_interval_s, settings.heartbeat_miss_factor
            ):
                registry.set_offline(d.device_id)
                db.upsert_device(d.device_id, {"status": "offline"})
                interval = d.heartbeat_interval_s or settings.default_heartbeat_interval_s
                logger.info(
                    "Device %s marked offline (no heartbeat for >%ss = %s× its %ss interval)",
                    d.device_id,
                    settings.heartbeat_miss_factor * interval,
                    settings.heartbeat_miss_factor,
                    interval,
                )
    except asyncio.CancelledError:
        pass


async def start_reaper() -> None:
    global _reaper_task
    _reaper_task = asyncio.create_task(_reaper_loop())
    logger.info(
        "Reaper started (per-device: %s× interval, default=%ss, sweep=%ss)",
        settings.heartbeat_miss_factor,
        settings.default_heartbeat_interval_s,
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
