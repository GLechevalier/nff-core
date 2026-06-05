from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

from nff_ota import fleet
from nff_ota.client import get_client
from nff_ota.models.deployment import (
    CreateDeploymentParams,
    DeploymentInfo,
    DeploymentResult,
    DeploymentStatus,
    DeploymentStatusResult,
    DeviceDeliveryResult,
    ReportDeviceStatusParams,
    StartDeploymentResult,
)
from nff_ota.storage import create_signed_url
from nff_ota.transitions import validate_transition

_TERMINAL_DD = {"success", "already-installed", "aborted", "decommissioned", "failure"}
_TERMINAL_DEP = {"finished", "aborted"}


async def create_deployment(params: CreateDeploymentParams) -> DeploymentResult:
    """Create a deployment of a firmware version to a device group."""
    client = get_client()

    # Validate the target firmware version exists and carries a deployable binary
    fw_resp = await client.table("firmware_versions").select("version, storage_path").eq("version", params.target_version).execute()
    if not fw_resp.data:
        return DeploymentResult(error=f"Firmware version {params.target_version!r} not found")
    if not fw_resp.data[0].get("storage_path"):
        return DeploymentResult(error=f"Firmware version {params.target_version!r} has no uploaded artifact")

    # Validate group exists and has accepted devices
    grp_resp = await client.table("device_groups").select("id").eq("id", params.device_group_id).execute()
    if not grp_resp.data:
        return DeploymentResult(error=f"Device group {params.device_group_id!r} not found")

    members_resp = (
        await client.table("device_group_members")
        .select("device_id")
        .eq("group_id", params.device_group_id)
        .execute()
    )
    member_ids = [r["device_id"] for r in (members_resp.data or [])]
    if not member_ids:
        return DeploymentResult(error="Device group has no members")

    accepted_resp = (
        await client.table("devices")
        .select("id, current_firmware_version, firmware_version")
        .in_("id", member_ids)
        .eq("ota_status", "accepted")
        .execute()
    )
    accepted = accepted_resp.data or []
    if not accepted:
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
        "target_version": params.target_version,
        "device_group_id": params.device_group_id,
        "status": initial_status,
        "max_devices_in_flight": params.max_devices_in_flight,
        "retries": params.retries,
        "created_at": now.isoformat(),
        "start_time": params.start_time,
    }
    dep_resp = await client.table("deployments").insert(dep_row).execute()

    # Create per-device job rows in ota_jobs
    job_rows = [
        {
            "id": str(uuid.uuid4()),
            "deployment_id": deployment_id,
            "device_id": dev["id"],
            "from_version": dev.get("current_firmware_version") or dev.get("firmware_version") or "unknown",
            "target_version": params.target_version,
            "status": "pending",
            "progress": 0,
            "attempts": 0,
            "started_at": now.isoformat(),
            "last_updated": now.isoformat(),
        }
        for dev in accepted
    ]
    await client.table("ota_jobs").insert(job_rows).execute()

    inserted = (dep_resp.data or [dep_row])[0]
    return DeploymentResult(deployment=_dep_row_to_info(inserted))


async def get_deployment_status(deployment_id: str) -> DeploymentStatusResult:
    """Get deployment status with per-device progress breakdown."""
    client = get_client()
    dep_resp = await client.table("deployments").select("*").eq("id", deployment_id).execute()
    if not dep_resp.data:
        return DeploymentStatusResult(error=f"Deployment {deployment_id!r} not found")

    jobs_resp = await client.table("ota_jobs").select("*").eq("deployment_id", deployment_id).execute()
    jobs = jobs_resp.data or []

    counts: dict[str, int] = {}
    for row in jobs:
        s = row["status"]
        counts[s] = counts.get(s, 0) + 1

    status = DeploymentStatus(
        deployment=_dep_row_to_info(dep_resp.data[0]),
        total_count=len(jobs),
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
    dep_resp = await client.table("deployments").select("*").eq("id", deployment_id).execute()
    if not dep_resp.data:
        return DeploymentResult(error=f"Deployment {deployment_id!r} not found")
    dep = dep_resp.data[0]
    try:
        validate_transition("deployment", dep["status"], "aborted")
    except ValueError as e:
        return DeploymentResult(error=str(e))

    await client.table("deployments").update({"status": "aborted"}).eq("id", deployment_id).execute()
    # Abort all non-terminal per-device jobs
    non_terminal = ["pending", "downloading", "installing", "rebooting"]
    await client.table("ota_jobs").update({"status": "aborted"}).eq("deployment_id", deployment_id).in_("status", non_terminal).execute()

    dep["status"] = "aborted"
    return DeploymentResult(deployment=_dep_row_to_info(dep))


async def list_deployments(status: str | None = None) -> list[DeploymentInfo]:
    """List deployments, optionally filtered by status."""
    client = get_client()
    query = client.table("deployments").select("*").order("created_at", desc=True)
    if status:
        query = query.eq("status", status)
    resp = await query.execute()
    return [_dep_row_to_info(r) for r in (resp.data or [])]


def _identity_attributes(row: dict) -> dict:
    attrs = row.get("identity_attributes") or {}
    if isinstance(attrs, str):
        attrs = json.loads(attrs)
    return attrs


async def start_deployment(deployment_id: str) -> StartDeploymentResult:
    """Push the deployment's firmware to its devices via nff-fleet.

    nff-ota decides what to push; nff-fleet delivers it. For each pending device job in
    the deployment, this builds an OTA command from the deployment's target firmware
    version (version, sha256, pre-signed download URL) and asks nff-fleet to sign +
    deliver it over MQTT (POST /internal/command). The device's MQTT identity is read
    from ``devices.identity_attributes['mqtt_id']``.

    Per-device state moves ``pending → downloading`` on dispatch; a delivery failure
    moves ``downloading → failure``. Final ``installing/rebooting/success`` and
    rollback handling arrive via the deferred ota_result feedback loop — not here.
    """
    client = get_client()

    dep_resp = await client.table("deployments").select("*").eq("id", deployment_id).execute()
    if not dep_resp.data:
        return StartDeploymentResult(error=f"Deployment {deployment_id!r} not found")
    dep = dep_resp.data[0]

    if dep["status"] not in ("scheduled", "active"):
        return StartDeploymentResult(
            error=f"Cannot start deployment in status {dep['status']!r}; must be scheduled or active"
        )

    # A scheduled deployment becomes active when we begin delivering.
    if dep["status"] == "scheduled":
        validate_transition("deployment", "scheduled", "active")
        await client.table("deployments").update({"status": "active"}).eq("id", deployment_id).execute()
        dep["status"] = "active"

    # Resolve the firmware this deployment ships.
    version = dep["target_version"]
    fw_resp = await client.table("firmware_versions").select("*").eq("version", version).execute()
    if not fw_resp.data:
        return StartDeploymentResult(error=f"Firmware version {version!r} not found")
    fw = fw_resp.data[0]
    sha256 = fw.get("checksum_sha256")
    if not sha256:
        return StartDeploymentResult(error=f"Firmware version {version!r} has no checksum / uploaded artifact")
    try:
        url = await create_signed_url(version)
    except Exception as e:
        return StartDeploymentResult(error=f"Failed to generate firmware URL: {e}")

    # Devices still waiting for this update.
    jobs_resp = (
        await client.table("ota_jobs")
        .select("*")
        .eq("deployment_id", deployment_id)
        .eq("status", "pending")
        .execute()
    )
    pending = jobs_resp.data or []
    if not pending:
        return StartDeploymentResult(
            deployment=_dep_row_to_info(dep), error="No pending devices to deliver to"
        )

    # Map device id -> fleet MQTT identity (cert CN).
    device_ids = [j["device_id"] for j in pending]
    dev_resp = (
        await client.table("devices").select("id, identity_attributes").in_("id", device_ids).execute()
    )
    mqtt_by_device = {
        d["id"]: _identity_attributes(d).get("mqtt_id") for d in (dev_resp.data or [])
    }

    sem = asyncio.Semaphore(max(1, dep["max_devices_in_flight"]))
    results: list[DeviceDeliveryResult] = []

    async def _deliver_one(job: dict) -> None:
        device_id = job["device_id"]
        mqtt_id = mqtt_by_device.get(device_id)
        if not mqtt_id:
            results.append(
                DeviceDeliveryResult(
                    device_id=device_id,
                    outcome="skipped",
                    detail="device has no identity_attributes.mqtt_id (fleet identity)",
                )
            )
            return
        async with sem:
            # Dispatch: pending -> downloading (counts as an attempt).
            now = datetime.now(timezone.utc).isoformat()
            await client.table("ota_jobs").update(
                {"status": "downloading", "attempts": job["attempts"] + 1, "last_updated": now}
            ).eq("id", job["id"]).execute()
            try:
                resp = await fleet.deliver_ota(mqtt_id, version, sha256, url)
                results.append(
                    DeviceDeliveryResult(
                        device_id=device_id, mqtt_id=mqtt_id, outcome="delivered",
                        detail=str(resp.get("status", "")),
                    )
                )
            except fleet.FleetError as e:
                await client.table("ota_jobs").update(
                    {"status": "failure", "last_updated": datetime.now(timezone.utc).isoformat()}
                ).eq("id", job["id"]).execute()
                results.append(
                    DeviceDeliveryResult(
                        device_id=device_id, mqtt_id=mqtt_id, outcome="failed", detail=str(e)
                    )
                )

    await asyncio.gather(*(_deliver_one(job) for job in pending))
    await _recompute_deployment_status(deployment_id)

    dep_after = await client.table("deployments").select("*").eq("id", deployment_id).execute()
    return StartDeploymentResult(
        deployment=_dep_row_to_info(dep_after.data[0]) if dep_after.data else _dep_row_to_info(dep),
        delivered=sum(1 for r in results if r.outcome == "delivered"),
        failed=sum(1 for r in results if r.outcome == "failed"),
        skipped=sum(1 for r in results if r.outcome == "skipped"),
        devices=results,
    )


async def report_device_status(params: ReportDeviceStatusParams) -> DeploymentResult:
    """Report a device's update progress.

    NOTE: with nff-ota driving delivery through nff-fleet (see start_deployment), the
    immediate response fleet returns is the device *ack* (often ``ota_started``), not
    the final outcome. Final ``success``/``rolled_back`` reconciliation from the
    device's later ``ota_result`` is the deferred feedback loop; until then this tool
    is the manual/explicit way to advance a device's job state.
    """
    client = get_client()

    # Find the per-device job row
    job_resp = (
        await client.table("ota_jobs")
        .select("*")
        .eq("device_id", params.device_id)
        .eq("deployment_id", params.deployment_id)
        .execute()
    )
    if not job_resp.data:
        return DeploymentResult(error=f"No active deployment {params.deployment_id!r} for device {params.device_id!r}")
    job = job_resp.data[0]

    try:
        validate_transition("device_deployment", job["status"], params.status)
    except ValueError as e:
        return DeploymentResult(error=str(e))

    # Increment attempts when entering downloading (retry detection)
    new_attempts = job["attempts"]
    if params.status == "downloading":
        new_attempts += 1

    now = datetime.now(timezone.utc).isoformat()
    update = {"status": params.status, "attempts": new_attempts, "last_updated": now}
    if params.status in ("success", "already-installed"):
        update["progress"] = 100
        update["completed_at"] = now
    await client.table("ota_jobs").update(update).eq("id", job["id"]).execute()

    # On success, update the device's current firmware version
    if params.status == "success":
        dep_resp = await client.table("deployments").select("target_version").eq("id", params.deployment_id).execute()
        if dep_resp.data:
            target_version = dep_resp.data[0]["target_version"]
            await client.table("devices").update({"current_firmware_version": target_version}).eq("id", params.device_id).execute()

    # Recompute deployment aggregate status
    await _recompute_deployment_status(params.deployment_id)

    dep_resp = await client.table("deployments").select("*").eq("id", params.deployment_id).execute()
    return DeploymentResult(deployment=_dep_row_to_info(dep_resp.data[0]) if dep_resp.data else None)


async def _recompute_deployment_status(deployment_id: str) -> None:
    client = get_client()
    dep_resp = await client.table("deployments").select("*").eq("id", deployment_id).execute()
    if not dep_resp.data:
        return
    dep = dep_resp.data[0]
    if dep["status"] in _TERMINAL_DEP:
        return

    jobs_resp = await client.table("ota_jobs").select("status").eq("deployment_id", deployment_id).execute()
    jobs = jobs_resp.data or []
    if not jobs:
        return

    statuses = [r["status"] for r in jobs]
    all_done = all(s in _TERMINAL_DD for s in statuses)
    if all_done:
        await client.table("deployments").update({"status": "finished"}).eq("id", deployment_id).execute()


def _dep_row_to_info(row: dict) -> DeploymentInfo:
    return DeploymentInfo(
        id=row["id"],
        name=row["name"],
        target_version=row["target_version"],
        device_group_id=row["device_group_id"],
        status=row["status"],
        max_devices_in_flight=row["max_devices_in_flight"],
        retries=row["retries"],
        created_at=str(row["created_at"]),
        start_time=str(row["start_time"]) if row.get("start_time") else None,
    )
