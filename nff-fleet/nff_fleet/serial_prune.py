"""Hourly retention pruner for device_serial_logs (wired-link console capture).

The dashboard browser appends console lines while a wired USB link is open;
nothing else bounds the table, so this loop deletes rows older than
settings.serial_log_retention_days. Same start/stop shape as reaper.py.
"""

from __future__ import annotations

import asyncio
import logging

from nff_fleet import db
from nff_fleet.config import settings

logger = logging.getLogger(__name__)

_prune_task: asyncio.Task | None = None


async def _prune_loop() -> None:
    try:
        while True:
            await asyncio.sleep(settings.serial_prune_interval)
            try:
                await db.prune_serial_logs(settings.serial_log_retention_days)
            except Exception as exc:  # noqa: BLE001 — retention must never kill the loop
                logger.warning("serial prune error: %s", exc)
    except asyncio.CancelledError:
        pass


async def start_serial_prune() -> None:
    global _prune_task
    _prune_task = asyncio.create_task(_prune_loop())
    logger.info(
        "Serial-log pruner started (retention=%sd, sweep=%ss)",
        settings.serial_log_retention_days,
        settings.serial_prune_interval,
    )


async def stop_serial_prune() -> None:
    global _prune_task
    if _prune_task is not None:
        _prune_task.cancel()
        try:
            await _prune_task
        except asyncio.CancelledError:
            pass
        _prune_task = None
