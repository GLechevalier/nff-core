from __future__ import annotations

import asyncio
import json
import logging
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

_TERMINAL_DD = {"success", "already-installed", "aborted", "decommissioned", "failure", "timed_out"}
_TERMINAL_DEP = {"finished", "aborted"}

logger = logging.getLogger(__name__)


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

    deployable, err = await _resolve_deployable_devices(
        params.device_group_id, params.target_version
    )
    if err:
        return DeploymentResult(error=err)

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
    await _insert_jobs(deployment_id, params.target_version, deployable)

    inserted = (dep_resp.data or [dep_row])[0]
    return DeploymentResult(deployment=_dep_row_to_info(inserted))


async def _resolve_accepted_devices(device_group_id: str) -> tuple[list[dict], str | None]:
    """Return a group's accepted member devices (id + firmware fields), or an error.

    Encapsulates the targeting rule shared by ``create_deployment`` and the poller:
    a device receives a job only if it is a member of the group AND has
    ``ota_status='accepted'``. Returns ([], reason) when the group has no members or
    none of them are accepted.
    """
    client = get_client()
    members_resp = (
        await client.table("device_group_members")
        .select("device_id")
        .eq("group_id", device_group_id)
        .execute()
    )
    member_ids = [r["device_id"] for r in (members_resp.data or [])]
    if not member_ids:
        return [], "Device group has no members"

    accepted_resp = (
        await client.table("devices")
        .select("id, current_firmware_version, firmware_version, device_type")
        .in_("id", member_ids)
        .eq("ota_status", "accepted")
        .execute()
    )
    accepted = accepted_resp.data or []
    if not accepted:
        return [], "Device group has no accepted devices"
    return accepted, None


async def group_member_device_types(group_id: str) -> list[str]:
    """Distinct self-attested device_types among a group's member devices.

    Lets the build-deploy path derive a firmware's target device_types from the group
    it ships to, instead of a caller-supplied list. Only members that have reported a
    ``device_type`` contribute; members with none are omitted (the deploy gate treats
    those as legacy/compatible regardless). Returns [] when the group is empty or no
    member has attested its hardware yet. Membership is taken regardless of ota_status:
    this answers "what hardware is in this group", not "what is deployable right now".
    """
    client = get_client()
    members_resp = (
        await client.table("device_group_members")
        .select("device_id")
        .eq("group_id", group_id)
        .execute()
    )
    member_ids = [r["device_id"] for r in (members_resp.data or [])]
    if not member_ids:
        return []
    devices_resp = (
        await client.table("devices")
        .select("device_type")
        .in_("id", member_ids)
        .execute()
    )
    # Dedup case-insensitively on the canonical vocabulary, preserving first-seen casing.
    seen: dict[str, str] = {}
    for row in devices_resp.data or []:
        dt = (row.get("device_type") or "").strip()
        if dt:
            seen.setdefault(dt.lower(), dt)
    return sorted(seen.values())


async def _firmware_device_types(target_version: str) -> list[str]:
    """The device_type allow-list an artifact declares it targets (may be empty).

    These are the human-entered (or connector-supplied) ``device_types`` recorded on
    the firmware_versions row. The deploy gate matches a device's self-attested
    ``device_type`` against this list. An empty/absent list means "no declared target",
    which the gate treats as ungated (deploy to all accepted).
    """
    client = get_client()
    resp = (
        await client.table("firmware_versions")
        .select("device_types")
        .eq("version", target_version)
        .execute()
    )
    if not resp.data:
        return []
    raw = resp.data[0].get("device_types") or []
    if isinstance(raw, str):
        raw = json.loads(raw)
    return [str(t) for t in raw]


def _filter_compatible(
    accepted: list[dict], artifact_device_types: list[str]
) -> tuple[list[dict], list[dict]]:
    """Split accepted devices into (compatible, skipped) against an artifact's targets.

    The deploy-time safety gate: a device only receives a job if its self-attested
    hardware ``device_type`` is compatible with the firmware. Rules:
      * artifact declares no device_types  -> ungated, everything is compatible.
      * device reports no device_type      -> compatible WITH a warning (legacy /
        not-yet-reflashed firmware; don't brick fleets mid-migration).
      * device_type in the allow-list      -> compatible.
      * device_type set but not in list    -> skipped (excluded from the rollout).
    Matching is case-insensitive on the canonical vocabulary (esp32, esp32s3, ...).
    """
    if not artifact_device_types:
        return accepted, []
    allowed = {t.strip().lower() for t in artifact_device_types if t and t.strip()}
    if not allowed:
        return accepted, []
    compatible: list[dict] = []
    skipped: list[dict] = []
    for dev in accepted:
        dt = (dev.get("device_type") or "").strip().lower()
        if not dt:
            logger.warning(
                "ota gate: device %s reports no device_type — allowing (legacy firmware)",
                dev.get("id"),
            )
            compatible.append(dev)
        elif dt in allowed:
            compatible.append(dev)
        else:
            skipped.append(dev)
    return compatible, skipped


async def _resolve_deployable_devices(
    device_group_id: str, target_version: str
) -> tuple[list[dict], str | None]:
    """Resolve a group's accepted members, then gate them by the artifact's device_types.

    The single source of truth for "who actually receives this firmware", shared by
    ``create_deployment`` and the poller's ``_materialize_jobs``. Returns
    (deployable_devices, error). An error means nothing deployable: either the group
    has no accepted members, or every accepted device is hardware-incompatible with the
    target firmware.
    """
    accepted, err = await _resolve_accepted_devices(device_group_id)
    if err:
        return [], err
    artifact_types = await _firmware_device_types(target_version)
    compatible, skipped = _filter_compatible(accepted, artifact_types)
    if skipped:
        logger.info(
            "ota gate: %d of %d accepted device(s) skipped as incompatible with "
            "firmware %s device_types %s",
            len(skipped), len(accepted), target_version, artifact_types,
        )
    if not compatible:
        group_types = sorted({(d.get("device_type") or "unknown") for d in accepted})
        return [], (
            f"No devices in the group are compatible with firmware device_types "
            f"{artifact_types}; accepted devices report types {group_types}"
        )
    return compatible, None


async def _insert_jobs(deployment_id: str, target_version: str, accepted: list[dict]) -> int:
    """Insert pending ota_jobs rows for the given accepted devices. Returns the count."""
    client = get_client()
    now = datetime.now(timezone.utc).isoformat()
    job_rows = [
        {
            "id": str(uuid.uuid4()),
            "deployment_id": deployment_id,
            "device_id": dev["id"],
            "from_version": dev.get("current_firmware_version") or dev.get("firmware_version") or "unknown",
            "target_version": target_version,
            "status": "pending",
            "progress": 0,
            "attempts": 0,
            "started_at": now,
            "last_updated": now,
        }
        for dev in accepted
    ]
    await client.table("ota_jobs").insert(job_rows).execute()
    return len(job_rows)


async def _materialize_jobs(
    deployment_id: str, target_version: str, device_group_id: str
) -> tuple[int, str | None]:
    """Create pending ota_jobs for a deployment from its group's accepted members.

    Used by the deployment poller for dashboard-created 'scheduled' deployment rows
    that have no jobs yet: the dashboard inserts only the deployment row, and job
    creation — with its accepted-device filter and from_version resolution — stays here
    as the single source of truth. Returns (jobs_created, error); 0 with an error means
    the group isn't deployable yet (no members / none accepted), which the poller leaves
    scheduled to retry on a later tick. The same device_type compatibility gate that
    ``create_deployment`` applies runs here too, so dashboard-created deployments can't
    bypass it.
    """
    deployable, err = await _resolve_deployable_devices(device_group_id, target_version)
    if err:
        return 0, err
    return await _insert_jobs(deployment_id, target_version, deployable), None


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

    # The version we put ON THE WIRE is the artifact's semver release_version, because the
    # device's anti-downgrade gate parses it as major.minor.patch and rejects anything else.
    # The catalog `version` (the firmware_versions PK, e.g. "repo-<sha7>") stays the job's
    # target_version. Fall back to the catalog version for manual/legacy artifacts whose
    # version is itself already a semver.
    ota_version = fw.get("release_version") or version
    # Sent alongside the OTA command (unsigned) so a software mock can adopt it and report
    # a matching heartbeat build_id; real firmware ignores it. None for legacy artifacts.
    ota_build_id = fw.get("build_id")

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

    # Map device id -> fleet MQTT identity (cert CN) and tenant scope (project_id).
    device_ids = [j["device_id"] for j in pending]
    dev_resp = (
        await client.table("devices").select("id, identity_attributes, project_id").in_("id", device_ids).execute()
    )
    mqtt_by_device = {
        d["id"]: _identity_attributes(d).get("mqtt_id") for d in (dev_resp.data or [])
    }
    project_by_device = {d["id"]: d.get("project_id") for d in (dev_resp.data or [])}

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
        project_id = project_by_device.get(device_id)
        if not project_id:
            results.append(
                DeviceDeliveryResult(
                    device_id=device_id,
                    outcome="skipped",
                    detail="device has no project_id (tenant scope) — re-provision it",
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
                resp = await fleet.deliver_ota(
                    mqtt_id, ota_version, sha256, url, project_id, build_id=ota_build_id
                )
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

    await _apply_job_status(
        job, params.status, params.deployment_id, params.device_id,
        extra_update={"attempts": new_attempts},
    )

    dep_resp = await client.table("deployments").select("*").eq("id", params.deployment_id).execute()
    return DeploymentResult(deployment=_dep_row_to_info(dep_resp.data[0]) if dep_resp.data else None)


async def _apply_job_status(
    job: dict,
    new_status: str,
    deployment_id: str,
    device_id: str,
    *,
    extra_update: dict | None = None,
) -> None:
    """Write a new status onto an ota_jobs row and apply its side effects.

    Shared by ``report_device_status`` (manual/explicit path, which validates the
    step transition first) and ``finalize_ota_result`` (device-reported authoritative
    outcome, which does NOT validate). On success-like states it stamps
    progress/completed_at and advances ``devices.current_firmware_version``; it always
    recomputes the parent deployment so it can roll up to ``finished``.
    """
    client = get_client()
    now = datetime.now(timezone.utc).isoformat()
    update = {"status": new_status, "last_updated": now}
    if extra_update:
        update.update(extra_update)
    if new_status in ("success", "already-installed"):
        update["progress"] = 100
        update["completed_at"] = now
    await client.table("ota_jobs").update(update).eq("id", job["id"]).execute()

    if new_status == "success":
        dep_resp = await client.table("deployments").select("target_version").eq("id", deployment_id).execute()
        if dep_resp.data:
            await client.table("devices").update(
                {"current_firmware_version": dep_resp.data[0]["target_version"]}
            ).eq("id", device_id).execute()

    await _recompute_deployment_status(deployment_id)


async def _resolve_device_id(reported_id: str) -> str | None:
    """Map a device-reported id (its fleet MQTT identity) to the nff-ota devices.id.

    Usually they're the same string (e.g. ``test-device-01``); fall back to matching
    ``identity_attributes->>mqtt_id`` for devices keyed differently.
    """
    client = get_client()
    direct = await client.table("devices").select("id").eq("id", reported_id).execute()
    if direct.data:
        return reported_id
    by_mqtt = await client.table("devices").select("id").eq("identity_attributes->>mqtt_id", reported_id).execute()
    if by_mqtt.data:
        return by_mqtt.data[0]["id"]
    return None


async def finalize_ota_result(device_id: str, status: str, version: str) -> dict:
    """Finalize an ota_jobs row from a device-reported ota_result.

    ``status`` is the device's authoritative outcome: ``committed`` -> ``success``,
    ``rolled_back`` -> ``failure``. ``device_id`` is what the device reported (its
    fleet MQTT identity); ``version`` is the firmware version it ran.

    Intentionally does NOT call ``validate_transition``: the job is parked in
    ``downloading`` (set at dispatch) and ``downloading -> success`` is a deliberately
    illegal *step* edge, but the device's reported terminal outcome is authoritative,
    so we set the terminal state directly. Idempotent and never raises for the webhook:
    a missing device or an already-terminal job returns ``{"finalized": False, ...}``.
    """
    client = get_client()
    new_status = "success" if status == "committed" else "failure"

    ota_device_id = await _resolve_device_id(device_id)
    if ota_device_id is None:
        logger.info("ota_result: no device matches %r — ignoring", device_id)
        return {"finalized": False, "reason": "device-not-found", "device_id": device_id}

    # The device reports the version it ran = the OTA command's wire version. For connector
    # deploys that's the artifact's semver release_version, NOT the job's catalog
    # target_version, so an exact target_version match won't find the job. Try the exact
    # match first (manual/legacy deploys where they're the same string), then fall back to
    # the device's single most-recent in-flight job — a device runs one OTA at a time.
    exact = (
        await client.table("ota_jobs")
        .select("*")
        .eq("device_id", ota_device_id)
        .eq("target_version", version)
        .order("last_updated", desc=True)
        .execute()
    )
    active = [j for j in (exact.data or []) if j["status"] not in _TERMINAL_DD]
    if not active:
        any_resp = (
            await client.table("ota_jobs")
            .select("*")
            .eq("device_id", ota_device_id)
            .order("last_updated", desc=True)
            .execute()
        )
        active = [j for j in (any_resp.data or []) if j["status"] not in _TERMINAL_DD]
    if not active:
        logger.info("ota_result: no active job for (%s, %s) — idempotent no-op", ota_device_id, version)
        return {"finalized": False, "reason": "no-active-job", "device_id": ota_device_id, "version": version}

    job = active[0]
    await _apply_job_status(job, new_status, job["deployment_id"], ota_device_id)
    logger.info("ota_result: device=%s version=%s -> %s (deployment=%s)",
                ota_device_id, version, new_status, job["deployment_id"])
    return {
        "finalized": True,
        "device_id": ota_device_id,
        "deployment_id": job["deployment_id"],
        "job_id": job["id"],
        "status": new_status,
    }


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
