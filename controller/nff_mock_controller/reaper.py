"""TTL/state reaper — tears down expired, exited, or orphaned mocks.

Mirrors ``nff-ota/nff_ota/reaper.py``: a resilient ``asyncio`` loop that ticks on an
interval (started from the Starlette lifespan, which lives for the whole server — unlike
the MCP per-request lifespan, so no separate thread is needed here). Every sweep is
scoped to ``nff.mock=1`` + ``mock-``-prefixed ids, so it can never touch a real device.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from nff_mock_controller import docker as dk
from nff_mock_controller import provision
from nff_mock_controller.config import settings

logger = logging.getLogger(__name__)

_MOCK_ID_PREFIX = "mock-"
_DEAD_STATES = {"exited", "dead", "removing"}

_task: asyncio.Task | None = None


def _expired(expires_at: str) -> bool:
    if not expires_at:
        return False
    try:
        exp = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= exp


async def sweep_once() -> int:
    """Reap expired/dead mocks. Returns the number torn down."""
    reaped = 0
    for m in await dk.list_mocks():
        if not m.device_id.startswith(_MOCK_ID_PREFIX):
            continue  # safety: only ever touch mock-spawned identities
        if not (m.state in _DEAD_STATES or _expired(m.expires_at)):
            continue
        await dk.kill_and_rm(m.name)
        try:
            await provision.deprovision(m.device_id)
        except Exception as exc:  # best-effort: a stuck deprovision must not wedge the sweep
            logger.warning("reaper: deprovision %s failed: %s", m.device_id, exc)
        reaped += 1

    if reaped:
        logger.info("reaper: tore down %d mock(s)", reaped)
    return reaped


async def _loop() -> None:
    while True:
        try:
            await sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let a transient docker/fleet error kill the loop
            logger.warning("reaper sweep error: %s", exc)
        await asyncio.sleep(settings.reaper_interval_s)


def start() -> None:
    global _task
    _task = asyncio.create_task(_loop())
    logger.info(
        "reaper started (ttl=%ss interval=%ss)", settings.mock_ttl_s, settings.reaper_interval_s
    )


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
    logger.info("reaper stopped")
