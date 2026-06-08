"""Deployment poller.

The nff-dashboard writes deployment rows directly to Supabase (status='scheduled')
instead of calling the ``start_deployment`` MCP tool. Nothing else would ever start
them, so this periodic sweep is what turns a dashboard "Push OTA" into real delivery:

  1. find deployments in ``scheduled`` / ``active``;
  2. honor a future ``start_time`` (leave it until it's due);
  3. for a scheduled deployment with no ``ota_jobs`` yet, materialize them from the
     device group's accepted members (``_materialize_jobs`` — the same logic
     ``create_deployment`` uses, kept in one place);
  4. run the existing ``start_deployment`` path, which asks nff-fleet to push firmware
     and moves jobs ``pending → downloading``.

A group with no accepted members is left ``scheduled`` for a later tick (devices may be
accepted afterwards) rather than aborted. A module-level in-flight set guards against a
slow tick overlapping itself; cross-process double-delivery (poller vs a manual
``start_deployment``) is bounded by the ``pending``-only job selection and is harmless
because the device treats a repeat OTA of the same version/sha256 as idempotent.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from nff_ota.client import get_client
from nff_ota.config import settings
from nff_ota.tools.deployments.ops import _materialize_jobs, start_deployment

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_in_flight: set[str] = set()


async def poll_once() -> int:
    """Start any due scheduled/active deployments. Returns the number started."""
    client = get_client()
    now = datetime.now(timezone.utc)

    resp = (
        await client.table("deployments")
        .select("*")
        .in_("status", ["scheduled", "active"])
        .execute()
    )
    deployments = resp.data or []
    started = 0

    for dep in deployments:
        dep_id = dep["id"]
        if dep_id in _in_flight:
            continue  # a previous (slow) tick is still working this one

        # Honor a future start_time — only meaningful while still scheduled.
        start_time = dep.get("start_time")
        if dep["status"] == "scheduled" and start_time:
            try:
                st = datetime.fromisoformat(str(start_time).replace("Z", "+00:00"))
            except ValueError:
                logger.warning("poller: deployment %s has invalid start_time %r — skipping", dep_id, start_time)
                continue
            if st > now:
                continue  # not yet due

        _in_flight.add(dep_id)
        try:
            # First time we see a scheduled, dashboard-created deployment: create its jobs.
            if dep["status"] == "scheduled":
                existing = (
                    await client.table("ota_jobs")
                    .select("id")
                    .eq("deployment_id", dep_id)
                    .limit(1)
                    .execute()
                )
                if not (existing.data or []):
                    created, err = await _materialize_jobs(dep_id, dep["target_version"], dep["device_group_id"])
                    if created == 0:
                        logger.info("poller: deployment %s not deployable yet (%s) — leaving scheduled", dep_id, err)
                        continue

            result = await start_deployment(dep_id)
            if result.error and result.error != "No pending devices to deliver to":
                logger.warning("poller: start_deployment(%s) error: %s", dep_id, result.error)
            elif not result.error:
                started += 1
        except Exception as exc:  # one bad deployment must not abort the whole sweep
            logger.warning("poller: error starting deployment %s: %s", dep_id, exc)
        finally:
            _in_flight.discard(dep_id)

    if started:
        logger.info("poller: started %d deployment(s)", started)
    return started


async def _loop() -> None:
    while True:
        try:
            await poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let a transient DB error kill the loop
            logger.warning("poller sweep error: %s", exc)
        await asyncio.sleep(settings.deployment_poller_interval)


def start() -> None:
    global _task
    if not settings.deployment_poller_enabled:
        logger.info("poller disabled (OTA_DEPLOYMENT_POLLER_ENABLED=false)")
        return
    _task = asyncio.create_task(_loop())
    logger.info("poller started (interval=%ss)", settings.deployment_poller_interval)


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
    logger.info("poller stopped")
