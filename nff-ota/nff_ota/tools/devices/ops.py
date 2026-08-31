from __future__ import annotations

import json
from datetime import datetime, timezone

from nff_ota.client import get_client
from nff_ota.models.device import DeviceInfo, DeviceResult, DeviceStatus, ListDevicesParams, PreauthorizeDeviceParams, RegisterDeviceParams
from nff_ota.transitions import validate_transition

# Defaults for the fleet NOT NULL columns when nff-ota creates a devices row for a
# device the heartbeat path has not seen yet. The heartbeat upsert will overwrite
# status/firmware_version once the device actually reports in.
_FLEET_DEFAULTS = {"status": "offline", "firmware_version": "unknown", "agent_connected": False}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def register_device(params: RegisterDeviceParams) -> DeviceResult:
    """Enroll a device for OTA by its real fleet id.

    If the device already exists (e.g. created by the fleet heartbeat), its enrollment
    fields are set; a device previously preauthorized is auto-accepted, otherwise it
    moves to 'pending'. If the device is unknown, a row is created in 'pending'.
    """
    client = get_client()
    enrollment = {
        "identity_attributes": params.identity_attributes,
        "public_key": params.public_key or None,
        "tags": params.tags,
    }

    existing = await client.table("devices").select("*").eq("id", params.device_id).execute()
    if existing.data:
        row = existing.data[0]
        target = "accepted" if row.get("ota_status") == "preauthorized" else "pending"
        update = {**enrollment, "ota_status": target}
        try:
            resp = await client.table("devices").update(update).eq("id", params.device_id).execute()
        except Exception as e:
            return DeviceResult(error=f"Database update failed: {e}")
        updated = (resp.data or [{**row, **update}])[0]
        return DeviceResult(device=_row_to_info(updated))

    row = {
        "id": params.device_id,
        "name": params.device_id,
        **_FLEET_DEFAULTS,
        **enrollment,
        "ota_status": "pending",
        "current_firmware_version": None,
        "created_at": _now(),
    }
    try:
        resp = await client.table("devices").insert(row).execute()
        inserted = (resp.data or [row])[0]
    except Exception as e:
        return DeviceResult(error=f"Database insert failed: {e}")
    return DeviceResult(device=_row_to_info(inserted))


async def accept_device(device_id: str) -> DeviceResult:
    """Accept a pending or rejected device."""
    return await _transition_device(device_id, "accepted")


async def reject_device(device_id: str) -> DeviceResult:
    """Reject a pending or accepted device."""
    return await _transition_device(device_id, "rejected")


async def decommission_device(device_id: str) -> DeviceResult:
    """Permanently decommission a device. Marks all its active deployments as decommissioned."""
    result = await _transition_device(device_id, "decommissioned")
    if result.error:
        return result

    # Mark any active per-device jobs for this device
    client = get_client()
    active_statuses = ["pending", "downloading", "installing", "rebooting"]
    await client.table("ota_jobs").update({"status": "decommissioned"}).eq("device_id", device_id).in_("status", active_statuses).execute()
    return result


async def preauthorize_device(params: PreauthorizeDeviceParams) -> DeviceResult:
    """Preauthorize a device so it is auto-accepted when it registers."""
    client = get_client()
    enrollment = {
        "identity_attributes": params.identity_attributes,
        "public_key": params.public_key or None,
        "ota_status": "preauthorized",
    }

    existing = await client.table("devices").select("*").eq("id", params.device_id).execute()
    if existing.data:
        try:
            resp = await client.table("devices").update(enrollment).eq("id", params.device_id).execute()
        except Exception as e:
            return DeviceResult(error=f"Database update failed: {e}")
        updated = (resp.data or [{**existing.data[0], **enrollment}])[0]
        return DeviceResult(device=_row_to_info(updated))

    row = {
        "id": params.device_id,
        "name": params.device_id,
        **_FLEET_DEFAULTS,
        **enrollment,
        "current_firmware_version": None,
        "tags": [],
        "created_at": _now(),
    }
    try:
        resp = await client.table("devices").insert(row).execute()
        inserted = (resp.data or [row])[0]
    except Exception as e:
        return DeviceResult(error=f"Database insert failed: {e}")
    return DeviceResult(device=_row_to_info(inserted))


async def list_devices(params: ListDevicesParams) -> list[DeviceInfo]:
    """List devices, optionally filtered by enrollment status or group membership."""
    client = get_client()
    query = client.table("devices").select("*").order("created_at", desc=True)
    if params.status:
        query = query.eq("ota_status", params.status)
    resp = await query.execute()
    devices = [_row_to_info(r) for r in (resp.data or [])]

    if params.group_id:
        member_resp = await client.table("device_group_members").select("device_id").eq("group_id", params.group_id).execute()
        member_ids = {r["device_id"] for r in (member_resp.data or [])}
        devices = [d for d in devices if d.id in member_ids]

    return devices


async def _transition_device(device_id: str, target: str) -> DeviceResult:
    client = get_client()
    resp = await client.table("devices").select("*").eq("id", device_id).execute()
    if not resp.data:
        return DeviceResult(error=f"Device {device_id!r} not found")
    row = resp.data[0]
    current = row.get("ota_status") or "pending"
    try:
        validate_transition("device", current, target)
    except ValueError as e:
        return DeviceResult(error=str(e))
    update_resp = await client.table("devices").update({"ota_status": target}).eq("id", device_id).execute()
    updated = (update_resp.data or [row])[0]
    updated["ota_status"] = target
    return DeviceResult(device=_row_to_info(updated))


def _row_to_info(row: dict) -> DeviceInfo:
    attrs = row.get("identity_attributes") or {}
    if isinstance(attrs, str):
        attrs = json.loads(attrs)
    tags = row.get("tags") or []
    if isinstance(tags, str):
        tags = json.loads(tags)
    return DeviceInfo(
        id=row["id"],
        identity_attributes=attrs,
        public_key=row.get("public_key"),
        status=row.get("ota_status") or "pending",
        current_firmware_version=row.get("current_firmware_version"),
        tags=tags,
        created_at=str(row.get("created_at") or ""),
    )
