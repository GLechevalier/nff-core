from __future__ import annotations

import uuid
from datetime import datetime, timezone

from nff_ota.client import get_client
from nff_ota.models.group import CreateGroupParams, GroupInfo, GroupResult, ModifyGroupMembersParams


async def create_group(params: CreateGroupParams) -> GroupResult:
    """Create a new device group."""
    client = get_client()
    group_id = str(uuid.uuid4())
    row = {
        "id": group_id,
        "name": params.name,
        "filter_criteria": params.filter_criteria,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        resp = await client.table("device_groups").insert(row).execute()
        inserted = (resp.data or [row])[0]
    except Exception as e:
        return GroupResult(error=f"Database insert failed: {e}")
    return GroupResult(group=_row_to_info(inserted, member_count=0))


async def add_devices_to_group(params: ModifyGroupMembersParams) -> GroupResult:
    """Add devices to a group (idempotent)."""
    client = get_client()
    grp_resp = await client.table("device_groups").select("*").eq("id", params.group_id).execute()
    if not grp_resp.data:
        return GroupResult(error=f"Group {params.group_id!r} not found")
    rows = [{"group_id": params.group_id, "device_id": did} for did in params.device_ids]
    try:
        await client.table("device_group_members").insert(rows).execute()
    except Exception:
        pass  # ignore duplicate-key errors (idempotent)
    count = await _member_count(params.group_id)
    return GroupResult(group=_row_to_info(grp_resp.data[0], member_count=count))


async def remove_devices_from_group(params: ModifyGroupMembersParams) -> GroupResult:
    """Remove devices from a group."""
    client = get_client()
    grp_resp = await client.table("device_groups").select("*").eq("id", params.group_id).execute()
    if not grp_resp.data:
        return GroupResult(error=f"Group {params.group_id!r} not found")
    await client.table("device_group_members").delete().eq("group_id", params.group_id).in_("device_id", params.device_ids).execute()
    count = await _member_count(params.group_id)
    return GroupResult(group=_row_to_info(grp_resp.data[0], member_count=count))


async def list_groups() -> list[GroupInfo]:
    """List all device groups with their member counts."""
    client = get_client()
    resp = await client.table("device_groups").select("*").order("created_at", desc=True).execute()
    result = []
    for row in (resp.data or []):
        count = await _member_count(row["id"])
        result.append(_row_to_info(row, member_count=count))
    return result


async def _member_count(group_id: str) -> int:
    client = get_client()
    resp = await client.table("device_group_members").select("device_id").eq("group_id", group_id).execute()
    return len(resp.data or [])


def _row_to_info(row: dict, member_count: int) -> GroupInfo:
    import json
    fc = row.get("filter_criteria") or {}
    if isinstance(fc, str):
        fc = json.loads(fc)
    return GroupInfo(
        id=row["id"],
        name=row["name"],
        filter_criteria=fc,
        member_count=member_count,
        created_at=str(row["created_at"]),
    )
