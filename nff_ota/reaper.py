"""Stuck-job reaper.

The OTA feedback loop relies on a device publishing an ``ota_result`` after it
reboots onto the new firmware. A device that downloads but never reports back
(bricked, or rolled back without reconnecting) would otherwise leave its
``ota_jobs`` row in ``downloading`` forever, so its deployment never finishes.

This periodic sweep marks any job that has sat in ``downloading`` longer than
``OTA_DOWNLOAD_TIMEOUT`` as ``timed_out`` (a terminal state) and recomputes the
parent deployment so it can roll up to ``finished``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from nff_ota.client import get_client
from nff_ota.config import settings
from nff_ota.tools.deployments.ops import _recompute_deployment_status

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None


async def sweep_once() -> int:
    """Time out jobs stuck in 'downloading'. Returns the number reaped."""
    client = get_client()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=settings.download_timeout)).isoformat()
    resp = (
        await client.table("ota_jobs")
        .select("*")
        .eq("status", "downloading")
        .lt("last_updated", cutoff)
        .execute()
    )
    jobs = resp.data or []
    if not jobs:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    deployment_ids: set[str] = set()
    for job in jobs:
        await client.table("ota_jobs").update(
            {"status": "timed_out", "last_updated": now}
        ).eq("id", job["id"]).execute()
        if job.get("deployment_id"):
            deployment_ids.add(job["deployment_id"])

    for dep_id in deployment_ids:
        await _recompute_deployment_status(dep_id)

    logger.info("reaper: timed out %d stuck job(s) across %d deployment(s)", len(jobs), len(deployment_ids))
    return len(jobs)


async def _loop() -> None:
    while True:
        try:
            await sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let a transient DB error kill the loop
            logger.warning("reaper sweep error: %s", exc)
        await asyncio.sleep(settings.reaper_interval)


def start() -> None:
    global _task
    _task = asyncio.create_task(_loop())
    logger.info("reaper started (timeout=%ss interval=%ss)", settings.download_timeout, settings.reaper_interval)


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
