from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from nff_ota.client import get_client
from nff_ota.models.device import DeviceInfo, DeviceResult, DeviceStatus, ListDevicesParams, PreauthorizeDeviceParams, RegisterDeviceParams
from nff_ota.transitions import validate_transition


async def register_device(params: RegisterDeviceParams) -> DeviceResult:
    """Register a new device. If matching preauthorized identity exists, auto-accept it."""
    client = get_client()
    canonical = json.dumps(params.identity_attributes, sort_keys=True)

    # Check for preauthorized match
    pre_resp = client.table("devices").select("*").eq("status", "preauthorized").execute()
    for row in (pre_resp.data or []):
        existing_attrs = row.get("identity_attributes") or {}
        if isinstance(existing_attrs, str):
            existing_attrs = json.loads(existing_attrs)
        if json.dumps(existing_attrs, sort_keys=True) == canonical:
            # Auto-accept
            update_resp = (
                client.table("devices")
                .update({"status": "accepted"})
                .eq("id", row["id"])
                .execute()
            )
            updated = (update_resp.data or [row])[0]
            updated["status"] = "accepted"
            return DeviceResult(device=_row_to_info(updated))

    device_id = str(uuid.uuid4())
    row = {
        "id": device_id,
        "identity_attributes": params.identity_attributes,
        "public_key": params.public_key or None,
        "status": "pending",
        "current_artifact_id": None,
        "tags": params.tags,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        resp = client.table("devices").insert(row).execute()
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

    # Mark any active device_deployments for this device
    client = get_client()
    active_dd_statuses = ["pending", "downloading", "installing", "rebooting"]
    client.table("device_deployments").update({"status": "decommissioned"}).eq("device_id", device_id).in_("status", active_dd_statuses).execute()
    return result


async def preauthorize_device(params: PreauthorizeDeviceParams) -> DeviceResult:
    """Preauthorize a device so it is auto-accepted when it registers."""
    client = get_client()
    device_id = str(uuid.uuid4())
    row = {
        "id": device_id,
        "identity_attributes": params.identity_attributes,
        "public_key": params.public_key or None,
        "status": "preauthorized",
        "current_artifact_id": None,
        "tags": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        resp = client.table("devices").insert(row).execute()
        inserted = (resp.data or [row])[0]
    except Exception as e:
        return DeviceResult(error=f"Database insert failed: {e}")
    return DeviceResult(device=_row_to_info(inserted))


async def list_devices(params: ListDevicesParams) -> list[DeviceInfo]:
    """List devices, optionally filtered by status or group membership."""
    client = get_client()
    query = client.table("devices").select("*").order("created_at", desc=True)
    if params.status:
        query = query.eq("status", params.status)
    resp = query.execute()
    devices = [_row_to_info(r) for r in (resp.data or [])]

    if params.group_id:
        member_resp = client.table("device_group_members").select("device_id").eq("group_id", params.group_id).execute()
        member_ids = {r["device_id"] for r in (member_resp.data or [])}
        devices = [d for d in devices if d.id in member_ids]

    return devices


async def _transition_device(device_id: str, target: str) -> DeviceResult:
    client = get_client()
    resp = client.table("devices").select("*").eq("id", device_id).execute()
    if not resp.data:
        return DeviceResult(error=f"Device {device_id!r} not found")
    row = resp.data[0]
    try:
        validate_transition("device", row["status"], target)
    except ValueError as e:
        return DeviceResult(error=str(e))
    update_resp = client.table("devices").update({"status": target}).eq("id", device_id).execute()
    updated = (update_resp.data or [row])[0]
    updated["status"] = target
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
        status=row["status"],
        current_artifact_id=row.get("current_artifact_id"),
        tags=tags,
        created_at=str(row["created_at"]),
    )
