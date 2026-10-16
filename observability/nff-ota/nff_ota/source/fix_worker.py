"""Background worker that runs enqueued firmware-fix jobs.

The ``submit_firmware_fix`` tool only enqueues a ``firmware_fix_jobs`` row and returns
immediately (see source/jobs.py). This worker is what actually compiles+deploys the fix —
the ~15-18 min L2 build. It runs on nff-ota's process-level sweeps loop (background.py),
NOT the MCP request loop, for the same reason the poller/reaper do: the streamable-http
lifespan is entered/exited per request, so a request-scoped task would be cancelled the
moment ``submit_firmware_fix`` returned and the compile would never run.

Each tick: reclaim any job stuck in 'running' (a worker that died mid-compile), then claim
the oldest pending job and run it to a terminal state. One job per tick keeps a single
worker from running several heavy compiles at once; with a short interval the claim latency
is negligible next to a multi-minute build.
"""

from __future__ import annotations

import asyncio
import logging

from nff_ota.config import settings
from nff_ota.source import jobs
from nff_ota.source.ops import build_and_deploy_source

logger = logging.getLogger(__name__)


async def _run_job(job: dict) -> None:
    """Compile + deploy a claimed job and write its terminal state."""
    job_id = job["id"]
    try:
        result = await build_and_deploy_source(
            job["device_id"],
            job["version"],
            job["source"],
            author=job.get("author") or "agent",
            parent_version=job.get("parent_version"),
            changelog=job.get("changelog"),
        )
    except Exception as exc:  # noqa: BLE001 — any build/deploy failure fails the job, not the loop
        logger.warning("fix_worker: job %s crashed: %s", job_id, exc)
        await jobs.finish(job_id, status="error", error=f"fix job crashed: {exc}")
        return

    if result.error:
        await jobs.finish(job_id, status="error", error=result.error)
        logger.info("fix_worker: job %s failed: %s", job_id, result.error)
    else:
        await jobs.finish(
            job_id,
            status="done",
            deployment_id=result.deployment_id,
            version=result.version,
            fault_remaining=result.fault_remaining,
        )
        logger.info(
            "fix_worker: job %s done (deployment=%s fault_remaining=%s)",
            job_id, result.deployment_id, result.fault_remaining,
        )


async def sweep_once() -> bool:
    """Reclaim stale jobs, then run at most one pending job. Returns True if one ran."""
    await jobs.reclaim_stale(settings.fix_job_stale_seconds, settings.fix_job_max_attempts)
    job = await jobs.claim_next()
    if job is None:
        return False
    logger.info(
        "fix_worker: claimed job %s for %s v%s", job["id"], job["device_id"], job["version"]
    )
    await _run_job(job)
    return True


async def _loop() -> None:
    while True:
        try:
            await sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let a transient DB error kill the loop
            logger.warning("fix_worker sweep error: %s", exc)
        await asyncio.sleep(settings.fix_worker_interval)
