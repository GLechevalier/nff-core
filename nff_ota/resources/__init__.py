from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from nff_ota.client import get_client


def register_resources(root: FastMCP) -> None:

    @root.resource("artifacts://v1/{artifact_id}", name="artifact-metadata", description="Firmware artifact metadata and checksum")
    async def artifact_resource(artifact_id: str) -> str:
        client = get_client()
        resp = client.table("artifacts").select("*").eq("id", artifact_id).execute()
        if not resp.data:
            return json.dumps({"error": f"Artifact {artifact_id!r} not found"})
        row = resp.data[0]
        row["device_types"] = row.get("device_types") or []
        row["provides"] = row.get("provides") or {}
        row["depends"] = row.get("depends") or {}
        return json.dumps(row, default=str)

    @root.resource("devices://v1/{device_id}", name="device-detail", description="Device identity, auth status, and recent deployment history")
    async def device_resource(device_id: str) -> str:
        client = get_client()
        dev_resp = client.table("devices").select("*").eq("id", device_id).execute()
        if not dev_resp.data:
            return json.dumps({"error": f"Device {device_id!r} not found"})
        device = dev_resp.data[0]
        device["identity_attributes"] = device.get("identity_attributes") or {}
        device["tags"] = device.get("tags") or []

        dd_resp = (
            client.table("device_deployments")
            .select("*")
            .eq("device_id", device_id)
            .order("last_updated", desc=True)
            .limit(10)
            .execute()
        )
        return json.dumps({"device": device, "recent_deployments": dd_resp.data or []}, default=str)

    @root.resource("deployments://v1/{deployment_id}", name="deployment-detail", description="Deployment status with per-device progress breakdown")
    async def deployment_resource(deployment_id: str) -> str:
        client = get_client()
        dep_resp = client.table("deployments").select("*").eq("id", deployment_id).execute()
        if not dep_resp.data:
            return json.dumps({"error": f"Deployment {deployment_id!r} not found"})

        dd_resp = client.table("device_deployments").select("*").eq("deployment_id", deployment_id).execute()
        dd_rows = dd_resp.data or []

        counts: dict[str, int] = {}
        for row in dd_rows:
            s = row["status"]
            counts[s] = counts.get(s, 0) + 1

        return json.dumps({
            "deployment": dep_resp.data[0],
            "total_count": len(dd_rows),
            "counts": counts,
            "devices": dd_rows,
        }, default=str)

    @root.resource("device-groups://v1/{group_id}", name="group-detail", description="Device group details with member count")
    async def group_resource(group_id: str) -> str:
        client = get_client()
        grp_resp = client.table("device_groups").select("*").eq("id", group_id).execute()
        if not grp_resp.data:
            return json.dumps({"error": f"Group {group_id!r} not found"})

        members_resp = client.table("device_group_members").select("device_id").eq("group_id", group_id).execute()
        member_count = len(members_resp.data or [])

        return json.dumps({
            "group": grp_resp.data[0],
            "member_count": member_count,
        }, default=str)
