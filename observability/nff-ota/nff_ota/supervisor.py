"""Phased / canary rollout supervisor (IOT_BRIDGE.md §3 gap #2).

A single deployment row spans a whole phased rollout; each ota_jobs row carries a ``wave``
(phase index). ``start_deployment`` delivers only wave 0. This periodic sweep watches each
active *phased* deployment and:

  * auto-advances — once the released cohort (wave <= current_phase) is fully terminal, under
    the deployment's ``failure_threshold``, and has soaked for ``soak_seconds``, it promotes to
    the next wave and delivers it;
  * auto-halts — if the released cohort's failure rate exceeds ``failure_threshold``, it pauses
    the deployment and records ``halted_reason``. Undelivered later waves stay ``pending`` (never
    sent), so the blast radius is capped; ``resume_deployment`` continues it later.

Runs on the same background thread/loop as the reaper and poller (see background.py) — it cannot
live on the MCP server's per-request lifespan loop.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from nff_ota.client import get_client
from nff_ota.config import settings
from nff_ota.tools.deployments.ops import (
    _FAILED_DD,
    _TERMINAL_DD,
    _deliver_wave,
    _parse_phases,
    _phase_decision,
)

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_in_flight: set[str] = set()  # deployments mid-tick, so a slow sweep can't double-process


async def _supervise_one(dep: dict) -> None:
    client = get_client()
    deployment_id = dep["id"]
    phases = _parse_phases(dep.get("phases"))
    if not phases:
        return  # not a phased deployment — nothing to supervise

    current_phase = int(dep.get("current_phase") or 0)
    threshold = float(dep.get("failure_threshold") or settings.default_failure_threshold)
    soak_seconds = int(dep.get("soak_seconds") or 0)

    # Released cohort: jobs in waves up to and including the current phase.
    jobs_resp = (
        await client.table("ota_jobs")
        .select("status, wave")
        .eq("deployment_id", deployment_id)
        .lte("wave", current_phase)
        .execute()
    )
    released = jobs_resp.data or []
    released_total = len(released)
    failures = sum(1 for j in released if j["status"] in _FAILED_DD)
    non_terminal = sum(1 for j in released if j["status"] not in _TERMINAL_DD)

    # Has the current wave soaked long enough? phase_entered_at is set on create / advance / resume.
    soak_elapsed = True
    entered = dep.get("phase_entered_at")
    if soak_seconds > 0 and entered:
        try:
            t0 = datetime.fromisoformat(str(entered).replace("Z", "+00:00"))
            soak_elapsed = (datetime.now(timezone.utc) - t0).total_seconds() >= soak_seconds
        except ValueError:
            soak_elapsed = True

    decision = _phase_decision(
        released_total=released_total,
        failures=failures,
        non_terminal=non_terminal,
        soak_elapsed=soak_elapsed,
        is_last_phase=current_phase >= len(phases) - 1,
        failure_threshold=threshold,
    )

    if decision == "halt":
        rate = failures / released_total if released_total else 0.0
        reason = (
            f"auto-halt at phase {current_phase} ({phases[current_phase]}%): "
            f"{failures}/{released_total} failed ({rate:.0%}) > threshold {threshold:.0%}"
        )
        await client.table("deployments").update(
            {"status": "paused", "halted_reason": reason}
        ).eq("id", deployment_id).execute()
        logger.warning("supervisor: %s — %s", deployment_id, reason)
        return

    if decision == "advance":
        next_phase = current_phase + 1
        now = datetime.now(timezone.utc).isoformat()
        await client.table("deployments").update(
            {"current_phase": next_phase, "phase_entered_at": now}
        ).eq("id", deployment_id).execute()
        dep = {**dep, "current_phase": next_phase, "phase_entered_at": now}
        result = await _deliver_wave(dep, deployment_id, next_phase)
        logger.info(
            "supervisor: %s advanced to phase %d (%d%%) — delivered=%d failed=%d skipped=%d",
            deployment_id, next_phase, phases[next_phase],
            result.delivered, result.failed, result.skipped,
        )
    # decision == "wait": still soaking or jobs in flight — nothing to do this tick.


async def sweep_once() -> int:
    """Supervise every active phased deployment. Returns how many were processed."""
    client = get_client()
    # Only phased deployments have anything to supervise. Filter server-side (phases IS NOT NULL)
    # and project just the columns _supervise_one / _deliver_wave read — this sweeps every
    # supervisor_interval, and pulling every active deployment's full row (most single-shot ones
    # only to discard them in Python) was pure egress. The Python guard below stays as a
    # belt-and-suspenders against an empty-string phases value.
    resp = (
        await client.table("deployments")
        .select(
            "id, status, target_version, phases, current_phase, failure_threshold, "
            "soak_seconds, phase_entered_at, max_devices_in_flight, project_id, rate_limit_override"
        )
        .eq("status", "active")
        .not_.is_("phases", "null")
        .execute()
    )
    deployments = [d for d in (resp.data or []) if d.get("phases")]
    processed = 0
    for dep in deployments:
        dep_id = dep["id"]
        if dep_id in _in_flight:
            continue
        _in_flight.add(dep_id)
        try:
            await _supervise_one(dep)
            processed += 1
        except Exception as exc:  # one bad deployment must not stall the others
            logger.warning("supervisor: error on %s: %s", dep_id, exc)
        finally:
            _in_flight.discard(dep_id)
    return processed


async def _loop() -> None:
    while True:
        try:
            await sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let a transient DB error kill the loop
            logger.warning("supervisor sweep error: %s", exc)
        await asyncio.sleep(settings.supervisor_interval)
