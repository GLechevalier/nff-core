"""End-of-deployment agent hand-off.

When a deployment reaches a terminal state (finished/aborted) we insert ONE
``role='user'`` row into ``agent_messages`` — the table nff-agent-worker already
watches (Realtime default; migration 0100's trigger mirrors the insert into
``agent_run_queue`` for queue-mode workers). The agent then summarizes the outcome
in the project's chat and, for non-pipeline failures, diagnoses and ships a fix.
Same single-insert coupling as nff-github's ``repair.py``; the hand-off must never
break deployment finalization, so the I/O entry point swallows every exception.

Pipeline-driven deployments (a ``pipeline_runs`` row references the deployment) get a
summary-ONLY prompt: nff-github's rollout watcher already dispatches the repair
hand-off for those, and two competing fix runs would race on the same repo.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime

from nff_ota.client import get_client
from nff_ota.config import settings

logger = logging.getLogger(__name__)

# Duplicated from tools/deployments/ops.py (ops imports this module — importing back
# would cycle). Keep in sync with ops._TERMINAL_DD / ops._FAILED_DD.
_TERMINAL_DD = {"success", "already-installed", "aborted", "decommissioned", "failure", "timed_out"}
_FAILED_DD = {"failure", "timed_out"}

# The marker the dashboard's chat transcript uses to hide an engineered preamble
# (nff-dashboard src/utils/chat.ts): everything BEFORE it is agent instructions,
# the short line AFTER it is what the user sees as "their" message.
USER_QUESTION_MARKER = "\n\nUser question: "

_MAX_FAILED_LISTED = 10
_MAX_REASON_CHARS = 300

# Per-project cooldown stamps for all-success/abort summaries (failures always emit).
# Module-level so the MCP loop and the background sweeps loop share it; best-effort
# (resets on restart) — the worker's per-project single-flight queue is the backstop.
_last_emit: dict[str, float] = {}


# --------------------------------------------------------------------------- #
# Pure prompt builders (no I/O — unit-tested directly)                         #
# --------------------------------------------------------------------------- #

def _status_counts(jobs: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for j in jobs:
        s = j.get("status") or "unknown"
        counts[s] = counts.get(s, 0) + 1
    return counts


def _failed_jobs(jobs: list[dict]) -> list[dict]:
    return [j for j in jobs if j.get("status") in _FAILED_DD]


def build_failure_playbook(dep: dict, jobs: list[dict], *, repo_full_name: str | None) -> str:
    """Diagnose+fix instructions for a NON-pipeline deployment with failed devices.

    Two fix paths: a project with a connected GitHub repo fixes at the source and opens a
    PR (nff-github repair.py playbook); a repo-less project (local/demo-style firmware)
    fixes through nff-ota's submit_firmware_fix pipeline (dashboard _selfheal.ts playbook).
    """
    failed = _failed_jobs(jobs)
    example_ids = ", ".join(f"`{j['device_id']}`" for j in failed[:2] if j.get("device_id"))
    intro = (
        f"\n\nDiagnose the failure first (e.g. device(s) {example_ids}): `fleet_get_crash` for a "
        f"failed device's panic, then `diagnose_serial` (its `raw` + `build_id`) or "
        f"`fleet_resolve_backtrace`; `fleet_get_diag` for live state. Then fix the firmware:\n"
    )
    if repo_full_name:
        fix = (
            f"1. Read the source with `read_repo_file` (e.g. the sketch `.ino`) to find the bug.\n"
            f"2. Make the smallest correct change.\n"
            f"3. Open the PR with `open_fix_pr(repo_full_name='{repo_full_name}', title=<NEXT "
            f"RELEASE SEMVER>, body=<what & why>, changes=[{{path, content}}])`. "
            f"The title MUST be a bare semver: take the current (failed) release and increment "
            f"its patch by exactly one (e.g. 3.0.18 → 3.0.19). It becomes the squash-merge commit "
            f"subject that re-triggers the build, and the device's anti-downgrade gate requires "
            f"it to be strictly greater than the current release.\n"
            f"4. Reply with a one-line diagnosis and the PR url."
        )
    else:
        fix = (
            "1. Read the running source with `get_firmware_source(device_id=<a failed device>)`.\n"
            "2. Make the smallest correct change to remove the bug, keeping the rest intact.\n"
            "3. Ship it with `submit_firmware_fix(device_id=..., version=<NEXT SEMVER>, "
            "source=<the full fixed source>)`. `version` MUST be a bare semver strictly greater "
            "than the version of the source you just read.\n"
            "4. `submit_firmware_fix` only ENQUEUES the build — it returns a `job_id` with "
            "`status='pending'`. Poll `get_firmware_fix_status(job_id=...)` until `status` is "
            "`done` or `error`. On `error`, read the `error` field, correct the source, and "
            "resubmit at a higher semver. Do NOT report success until you have seen "
            "`status='done'` with `fault_remaining` false."
        )
    return intro + fix


def build_summary_prompt(
    dep: dict,
    jobs: list[dict],
    *,
    repo_ctx: str | None = None,
    pipeline_managed: bool = False,
    playbook: str | None = None,
) -> str:
    """Assemble the full hand-off prompt (summary block, optional playbook, marker line)."""
    counts = _status_counts(jobs)
    failed = _failed_jobs(jobs)
    total = len(jobs)
    name = dep.get("name") or dep.get("id") or "deployment"
    final_status = dep.get("status") or "finished"

    status_line = final_status
    if dep.get("halted_reason"):
        status_line += f" — {dep['halted_reason']}"

    lines = [
        "An OTA deployment in this project has ended. Summarize the outcome for the user in "
        "2-4 sentences (what shipped, to whom, how it went). Be concrete; do not invent data.",
        "",
        f"Deployment: \"{name}\" (id {dep.get('id')})",
        f"Target version: {dep.get('target_version')}   Device group: {dep.get('device_group_id')}",
        f"Final status: {status_line}",
        f"Devices: {total} total — {counts.get('success', 0)} succeeded, "
        f"{counts.get('already-installed', 0)} already installed, "
        f"{counts.get('failure', 0)} failed, {counts.get('timed_out', 0)} timed out, "
        f"{counts.get('aborted', 0)} aborted, {counts.get('decommissioned', 0)} decommissioned",
    ]
    if dep.get("phases"):
        lines.append(
            f"Phased rollout: phases {dep['phases']}, ended in phase {dep.get('current_phase')}"
        )
    if failed:
        listed = []
        for j in failed[:_MAX_FAILED_LISTED]:
            reason = str(j.get("failure_reason") or j.get("status"))[:_MAX_REASON_CHARS]
            listed.append(f"{j.get('device_id')} — {reason}")
        overflow = len(failed) - _MAX_FAILED_LISTED
        suffix = f" (+{overflow} more)" if overflow > 0 else ""
        lines.append(f"Failed devices (first {_MAX_FAILED_LISTED}): " + "; ".join(listed) + suffix)
    started = dep.get("start_time") or dep.get("created_at")
    if started:
        lines.append(f"Started: {started}")

    if pipeline_managed and repo_ctx:
        lines.append("")
        lines.append(f"This deployment was driven by the nff-github deploy pipeline ({repo_ctx}).")
    if pipeline_managed and failed:
        lines.append(
            "A separate repair task has already been dispatched by the pipeline — ONLY summarize "
            "here; do NOT open a PR and do NOT submit a firmware fix."
        )

    prompt = "\n".join(lines)
    if playbook:
        prompt += playbook
        marker_line = (
            f"OTA deployment '{name}' ended with {len(failed)}/{total} device failures — "
            f"summarize, diagnose and fix."
        )
    else:
        marker_line = (
            f"Summarize the outcome of OTA deployment '{name}' ({final_status}, "
            f"{counts.get('success', 0)}/{total} succeeded)."
        )
    return prompt + USER_QUESTION_MARKER + marker_line


# --------------------------------------------------------------------------- #
# I/O entry point                                                              #
# --------------------------------------------------------------------------- #

async def notify_deployment_terminal(deployment_id: str, final_status: str) -> None:
    """Hand a just-terminal deployment to the agent. Never raises.

    Callers (ops._recompute_deployment_status via CAS, ops.abort_deployment via the
    transition gate) guarantee single-shot invocation per deployment.
    """
    try:
        if not settings.agent_on_finish:
            return
        client = get_client()

        dep_resp = await client.table("deployments").select("*").eq("id", deployment_id).execute()
        if not dep_resp.data:
            return
        dep = dict(dep_resp.data[0])
        dep["status"] = final_status
        project_id = dep.get("project_id")
        if not project_id:  # agent_messages.project_id is NOT NULL — nothing to hand off to
            return

        if settings.agent_on_finish_skip_demo:
            proj_resp = (
                await client.table("projects")
                .select("is_demo,is_pool")
                .eq("id", project_id)
                .execute()
            )
            if proj_resp.data and (
                proj_resp.data[0].get("is_demo") or proj_resp.data[0].get("is_pool")
            ):
                return

        jobs_resp = (
            await client.table("ota_jobs")
            .select("device_id,status,failure_reason,wave")
            .eq("deployment_id", deployment_id)
            .execute()
        )
        jobs = jobs_resp.data or []
        failed = _failed_jobs(jobs)

        # Cooldown throttles routine all-success/abort summaries per project; a failure is
        # actionable and always emits.
        cooldown = settings.agent_on_finish_cooldown_seconds
        if not failed and cooldown > 0:
            last = _last_emit.get(project_id)
            if last is not None and (time.monotonic() - last) < cooldown:
                return

        run_resp = (
            await client.table("pipeline_runs")
            .select("id,commit_sha,connection_id")
            .eq("deployment_id", deployment_id)
            .limit(1)
            .execute()
        )
        run = run_resp.data[0] if run_resp.data else None
        pipeline_managed = run is not None

        repo_ctx: str | None = None
        fix_repo: str | None = None
        if pipeline_managed and run.get("connection_id"):
            conn_resp = (
                await client.table("github_connections")
                .select("repo_full_name")
                .eq("id", run["connection_id"])
                .execute()
            )
            repo = conn_resp.data[0]["repo_full_name"] if conn_resp.data else "?"
            sha7 = str(run.get("commit_sha") or "")[:7]
            repo_ctx = f"repo {repo}@{sha7}, run {run['id']}"
        elif not pipeline_managed and failed:
            conn_resp = (
                await client.table("github_connections")
                .select("repo_full_name")
                .eq("project_id", project_id)
                .limit(1)
                .execute()
            )
            if conn_resp.data:
                fix_repo = conn_resp.data[0]["repo_full_name"]

        playbook = None
        if failed and not pipeline_managed:
            playbook = build_failure_playbook(dep, jobs, repo_full_name=fix_repo)

        prompt = build_summary_prompt(
            dep, jobs, repo_ctx=repo_ctx, pipeline_managed=pipeline_managed, playbook=playbook
        )

        # agent_messages.id is TEXT with NO DB default — the inserter must supply it
        # (same contract nff-github repair.py documents). No session_id: the summary
        # lands in the project's main chat.
        await client.table("agent_messages").insert(
            {
                "id": str(uuid.uuid4()),
                "role": "user",
                "content": prompt,
                "ts": datetime.now(UTC).isoformat(),
                "project_id": project_id,
            }
        ).execute()
        if not failed:
            _last_emit[project_id] = time.monotonic()
        logger.info(
            "agent_notify: handed off deployment %s (%s, %d/%d failed)",
            deployment_id, final_status, len(failed), len(jobs),
        )
    except Exception as exc:  # never let the hand-off break deployment finalization
        logger.warning("agent_notify: hand-off for %s failed: %s", deployment_id, exc)
