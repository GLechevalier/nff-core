from __future__ import annotations

import os

import httpx

from nff_diagnosis.fleet.roster.base import FleetRosterBase
from nff_diagnosis.schemas import FleetDevice

_SELECT = "id,status,firmware_version,build_id,project_id,last_seen,device_type,tags"


class FleetRosterNotConfigured(RuntimeError):
    pass


class SupabaseFleetRoster(FleetRosterBase):
    """Reads the shared Supabase `devices` table over PostgREST — the same table
    nff-fleet writes and nff-dashboard reads. Prefers the service-role key so
    server-side reads bypass row-level security; falls back to the anon key.
    """

    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._url = (url or os.getenv("SUPABASE_URL") or "").rstrip("/")
        self._key = (
            key
            or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
            or os.getenv("SUPABASE_ANON_KEY")
            or ""
        )
        self._timeout = timeout
        if not self._url or not self._key:
            raise FleetRosterNotConfigured(
                "Supabase fleet roster needs SUPABASE_URL and "
                "SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_ANON_KEY). "
                "Set them, or pass a roster JSON file instead."
            )

    @staticmethod
    def _to_device(row: dict) -> FleetDevice:
        tags = row.get("tags")
        if not isinstance(tags, list):
            tags = []
        return FleetDevice(
            device_id=row["id"],
            firmware_version=row.get("firmware_version"),
            build_id=row.get("build_id"),
            project_id=row.get("project_id"),
            status=row.get("status"),
            last_seen=row.get("last_seen"),
            device_type=row.get("device_type"),
            tags=tags,
        )

    def list_devices(
        self,
        build_id: str | None = None,
        firmware_version: str | None = None,
        project_id: str | None = None,
    ) -> list[FleetDevice]:
        params = {"select": _SELECT}
        if build_id is not None:
            params["build_id"] = f"eq.{build_id}"
        if firmware_version is not None:
            params["firmware_version"] = f"eq.{firmware_version}"
        if project_id is not None:
            params["project_id"] = f"eq.{project_id}"

        headers = {"apikey": self._key, "Authorization": f"Bearer {self._key}"}
        resp = httpx.get(
            f"{self._url}/rest/v1/devices",
            params=params,
            headers=headers,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return [self._to_device(row) for row in resp.json()]
