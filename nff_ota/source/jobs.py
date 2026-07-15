"""Persistence for the async firmware-fix job queue (``firmware_fix_jobs``).

Split out from ``source/ops.py`` so the submit path (enqueue + read) and the background
worker (``source/fix_worker.py``: claim + finish) share one place that knows the table
shape. All writes are service-role (RLS-bypassing); see migration 0069.

Flow: ``submit_firmware_fix`` calls :func:`enqueue` and returns the id immediately; the
agent polls :func:`get` (surfaced as the ``get_firmware_fix_status`` tool); the worker
:func:`claim_next` / :func:`finish` a job around the long compile.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from nff_ota.client import get_client
from nff_ota.models.source import FirmwareFixJob


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_model(row: dict) -> FirmwareFixJob:
    """Project a ``firmware_fix_jobs`` row onto the agent-facing job model."""
    return FirmwareFixJob(
        job_id=row["id"],
        status=row["status"],
        device_id=row.get("device_id"),
        version=row.get("version"),
        deployment_id=row.get("deployment_id"),
        fault_remaining=bool(row.get("fault_remaining")),
        error=row.get("error"),
    )


async def enqueue(
    device_id: str,
    version: str,
    source: str,
    *,
    project_id: str | None,
    author: str = "agent",
    parent_version: str | None = None,
    changelog: str | None = None,
) -> str:
    """Insert a pending job and return its id. Raises on a DB error."""
    client = get_client()
    job_id = str(uuid.uuid4())
    await client.table("firmware_fix_jobs").insert(
        {
            "id": job_id,
            "project_id": project_id,
            "device_id": device_id,
            "version": version,
            "source": source,
            "parent_version": parent_version,
            "changelog": changelog,
            "author": author,
            "status": "pending",
        }
    ).execute()
    return job_id


async def get(job_id: str) -> FirmwareFixJob | None:
    """Read a job by id, or None if it doesn't exist."""
    client = get_client()
    resp = (
        await client.table("firmware_fix_jobs")
        .select("*")
        .eq("id", job_id)
        .limit(1)
        .execute()
    )
    return to_model(resp.data[0]) if resp.data else None


async def reclaim_stale(stale_seconds: float, max_attempts: int) -> int:
    """Recover jobs stuck in 'running' (a worker died mid-compile).

    A job claimed but not finished within ``stale_seconds`` is re-queued to 'pending' so
    another worker picks it up — unless it has already been claimed ``max_attempts`` times,
    in which case it is failed terminally (something about it keeps killing the worker).
    Returns the number of stale jobs touched.
    """
    client = get_client()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat()
    resp = (
        await client.table("firmware_fix_jobs")
        .select("id, attempts")
        .eq("status", "running")
        .lt("started_at", cutoff)
        .execute()
    )
    stale = resp.data or []
    for job in stale:
        if (job.get("attempts") or 0) >= max_attempts:
            await client.table("firmware_fix_jobs").update(
                {
                    "status": "error",
                    "error": f"compile did not finish after {max_attempts} attempt(s) "
                    "(worker restarted mid-build)",
                    "finished_at": _now(),
                    "updated_at": _now(),
                }
            ).eq("id", job["id"]).eq("status", "running").execute()
        else:
            await client.table("firmware_fix_jobs").update(
                {"status": "pending", "updated_at": _now()}
            ).eq("id", job["id"]).eq("status", "running").execute()
    return len(stale)


async def claim_next() -> dict | None:
    """Atomically claim the oldest pending job. Returns the claimed row, or None.

    The claim is a conditional update (``status == 'pending'``): if another worker won the
    race the update matches no row and we return None so the caller moves on.

    The probe selects only ``id, attempts`` — this runs every ``fix_worker_interval`` (~3s),
    and the full row carries the firmware ``source`` blob, which we must not re-egress on every
    idle tick. The heavy row is fetched once, only after a claim is actually won.
    """
    client = get_client()
    resp = (
        await client.table("firmware_fix_jobs")
        .select("id, attempts")
        .eq("status", "pending")
        .order("created_at")
        .limit(1)
        .execute()
    )
    if not resp.data:
        return None
    job = resp.data[0]
    upd = (
        await client.table("firmware_fix_jobs")
        .update(
            {
                "status": "running",
                "started_at": _now(),
                "attempts": (job.get("attempts") or 0) + 1,
                "updated_at": _now(),
            }
        )
        .eq("id", job["id"])
        .eq("status", "pending")
        .execute()
    )
    if not upd.data:
        return None  # lost the race to another worker
    # We won the claim — now (and only now) read the full row, including the ``source`` the
    # worker needs to compile. status is already 'running' in the DB after the update above.
    full = (
        await client.table("firmware_fix_jobs")
        .select("*")
        .eq("id", job["id"])
        .limit(1)
        .execute()
    )
    if not full.data:
        return None  # vanished between claim and read (extremely unlikely) — let the caller retry
    return full.data[0]


async def finish(
    job_id: str,
    *,
    status: str,
    deployment_id: str | None = None,
    version: str | None = None,
    fault_remaining: bool = False,
    error: str | None = None,
) -> None:
    """Write a job's terminal state (done|error)."""
    client = get_client()
    update: dict = {
        "status": status,
        "fault_remaining": fault_remaining,
        "error": error,
        "finished_at": _now(),
        "updated_at": _now(),
    }
    if deployment_id is not None:
        update["deployment_id"] = deployment_id
    if version is not None:
        update["version"] = version
    await client.table("firmware_fix_jobs").update(update).eq("id", job_id).execute()
