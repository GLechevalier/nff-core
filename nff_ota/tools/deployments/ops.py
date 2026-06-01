from __future__ import annotations

import uuid
from datetime import datetime, timezone

from nff_ota.client import get_client
from nff_ota.config import settings
from nff_ota.models.deployment import (
    CreateDeploymentParams,
    DeploymentInfo,
    DeploymentResult,
    DeploymentStatus,
    DeploymentStatusResult,
    DeviceDeploymentInfo,
    ReportDeviceStatusParams,
)
from nff_ota.transitions import validate_transition

_TERMINAL_DD = {"success", "already-installed", "aborted", "decommissioned", "failure"}
_TERMINAL_DEP = {"finished", "aborted"}


async def create_deployment(params: CreateDeploymentParams) -> DeploymentResult:
    """Create a deployment of an artifact to a device group."""
    client = get_client()

    # Validate artifact exists
    art_resp = client.table("artifacts").select("id").eq("id", params.artifact_id).execute()
    if not art_resp.data:
        return DeploymentResult(error=f"Artifact {params.artifact_id!r} not found")

    # Validate group exists and has accepted devices
    grp_resp = client.table("device_groups").select("id").eq("id", params.device_group_id).execute()
    if not grp_resp.data:
        return DeploymentResult(error=f"Device group {params.device_group_id!r} not found")

    members_resp = (
        client.table("device_group_members")
        .select("device_id")
        .eq("group_id", params.device_group_id)
        .execute()
    )
    member_ids = [r["device_id"] for r in (members_resp.data or [])]
    if not member_ids:
        return DeploymentResult(error="Device group has no members")

    accepted_resp = (
        client.table("devices")
        .select("id")
        .in_("id", member_ids)
        .eq("status", "accepted")
        .execute()
    )
    accepted_ids = [r["id"] for r in (accepted_resp.data or [])]
    if not accepted_ids:
        return DeploymentResult(error="Device group has no accepted devices")

    # Determine initial deployment status
    now = datetime.now(timezone.utc)
    initial_status = "scheduled"
    if not params.start_time:
        initial_status = "active"
    else:
        try:
            st = datetime.fromisoformat(params.start_time.replace("Z", "+00:00"))
            if st <= now:
                initial_status = "active"
        except ValueError:
            return DeploymentResult(error=f"Invalid start_time format: {params.start_time!r}")

    deployment_id = str(uuid.uuid4())
    dep_row = {
        "id": deployment_id,
        "name": params.name,
        "artifact_id": params.artifact_id,
        "device_group_id": params.device_group_id,
        "status": initial_status,
        "max_devices_in_flight": params.max_devices_in_flight,
        "retries": params.retries,
        "created_at": now.isoformat(),
        "start_time": params.start_time,
    }
    dep_resp = client.table("deployments").insert(dep_row).execute()
    if dep_resp.error:
        return DeploymentResult(error=str(dep_resp.error))

    # Create per-device deployment rows
    dd_rows = [
        {
            "id": str(uuid.uuid4()),
            "deployment_id": deployment_id,
            "device_id": did,
            "status": "pending",
            "attempts": 0,
            "last_updated": now.isoformat(),
        }
        for did in accepted_ids
    ]
    client.table("device_deployments").insert(dd_rows).execute()

    inserted = (dep_resp.data or [dep_row])[0]
    return DeploymentResult(deployment=_dep_row_to_info(inserted))


async def get_deployment_status(deployment_id: str) -> DeploymentStatusResult:
    """Get deployment status with per-device progress breakdown."""
    client = get_client()
    dep_resp = client.table("deployments").select("*").eq("id", deployment_id).execute()
    if not dep_resp.data:
        return DeploymentStatusResult(error=f"Deployment {deployment_id!r} not found")

    dd_resp = client.table("device_deployments").select("*").eq("deployment_id", deployment_id).execute()
    dd_rows = dd_resp.data or []

    counts: dict[str, int] = {}
    for row in dd_rows:
        s = row["status"]
        counts[s] = counts.get(s, 0) + 1

    status = DeploymentStatus(
        deployment=_dep_row_to_info(dep_resp.data[0]),
        total_count=len(dd_rows),
        pending_count=counts.get("pending", 0),
        downloading_count=counts.get("downloading", 0),
        installing_count=counts.get("installing", 0),
        rebooting_count=counts.get("rebooting", 0),
        success_count=counts.get("success", 0),
        failure_count=counts.get("failure", 0),
        aborted_count=counts.get("aborted", 0),
        already_installed_count=counts.get("already-installed", 0),
    )
    return DeploymentStatusResult(status=status)


async def abort_deployment(deployment_id: str) -> DeploymentResult:
    """Abort an active or scheduled deployment."""
    client = get_client()
    dep_resp = client.table("deployments").select("*").eq("id", deployment_id).execute()
    if not dep_resp.data:
        return DeploymentResult(error=f"Deployment {deployment_id!r} not found")
    dep = dep_resp.data[0]
    try:
        validate_transition("deployment", dep["status"], "aborted")
    except ValueError as e:
        return DeploymentResult(error=str(e))

    client.table("deployments").update({"status": "aborted"}).eq("id", deployment_id).execute()
    # Abort all non-terminal device_deployments
    non_terminal = ["pending", "downloading", "installing", "rebooting"]
    client.table("device_deployments").update({"status": "aborted"}).eq("deployment_id", deployment_id).in_("status", non_terminal).execute()

    dep["status"] = "aborted"
    return DeploymentResult(deployment=_dep_row_to_info(dep))


async def list_deployments(status: str | None = None) -> list[DeploymentInfo]:
    """List deployments, optionally filtered by status."""
    client = get_client()
    query = client.table("deployments").select("*").order("created_at", desc=True)
    if status:
        query = query.eq("status", status)
    resp = query.execute()
    return [_dep_row_to_info(r) for r in (resp.data or [])]


async def report_device_status(params: ReportDeviceStatusParams) -> DeploymentResult:
    """Called by a device agent to report its update progress."""
    client = get_client()

    # Find the device_deployment row
    dd_resp = (
        client.table("device_deployments")
        .select("*")
        .eq("device_id", params.device_id)
        .eq("deployment_id", params.deployment_id)
        .execute()
    )
    if not dd_resp.data:
        return DeploymentResult(error=f"No active deployment {params.deployment_id!r} for device {params.device_id!r}")
    dd = dd_resp.data[0]

    try:
        validate_transition("device_deployment", dd["status"], params.status)
    except ValueError as e:
        return DeploymentResult(error=str(e))

    # Increment attempts when entering downloading (retry detection)
    new_attempts = dd["attempts"]
    if params.status == "downloading":
        new_attempts += 1

    now = datetime.now(timezone.utc).isoformat()
    client.table("device_deployments").update({
        "status": params.status,
        "attempts": new_attempts,
        "last_updated": now,
    }).eq("id", dd["id"]).execute()

    # On success, update device's current_artifact_id
    if params.status == "success":
        dep_resp = client.table("deployments").select("artifact_id").eq("id", params.deployment_id).execute()
        if dep_resp.data:
            artifact_id = dep_resp.data[0]["artifact_id"]
            client.table("devices").update({"current_artifact_id": artifact_id}).eq("id", params.device_id).execute()

    # Recompute deployment aggregate status
    await _recompute_deployment_status(params.deployment_id)

    dep_resp = client.table("deployments").select("*").eq("id", params.deployment_id).execute()
    return DeploymentResult(deployment=_dep_row_to_info(dep_resp.data[0]) if dep_resp.data else None)


async def _recompute_deployment_status(deployment_id: str) -> None:
    client = get_client()
    dep_resp = client.table("deployments").select("*").eq("id", deployment_id).execute()
    if not dep_resp.data:
        return
    dep = dep_resp.data[0]
    if dep["status"] in _TERMINAL_DEP:
        return

    dd_resp = client.table("device_deployments").select("*").eq("deployment_id", deployment_id).execute()
    dd_rows = dd_resp.data or []
    if not dd_rows:
        return

    statuses = [r["status"] for r in dd_rows]
    retries_limit = dep["retries"]

    all_done = all(s in {"success", "already-installed", "aborted", "decommissioned", "failure"} for s in statuses)
    if all_done:
        client.table("deployments").update({"status": "finished"}).eq("id", deployment_id).execute()


def _dep_row_to_info(row: dict) -> DeploymentInfo:
    return DeploymentInfo(
        id=row["id"],
        name=row["name"],
        artifact_id=row["artifact_id"],
        device_group_id=row["device_group_id"],
        status=row["status"],
        max_devices_in_flight=row["max_devices_in_flight"],
        retries=row["retries"],
        created_at=str(row["created_at"]),
        start_time=str(row["start_time"]) if row.get("start_time") else None,
    )
