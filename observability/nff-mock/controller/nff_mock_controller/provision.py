"""Client for fleet's internal provisioning plane.

The controller must never hold the CA signing key, so it asks fleet to mint/remove a
per-device mTLS cert via the shared-secret endpoints added to
``nff-fleet/nff_fleet/control.py``. Modeled on ``nff-ota/nff_ota/fleet.py``.

    POST {fleet_url}/internal/provision     {"device_id": "<id>"}
    POST {fleet_url}/internal/deprovision   {"device_id": "<id>"}
        Header: X-Fleet-Secret: <shared secret>
"""

from __future__ import annotations

from typing import Any

import httpx

from nff_mock_controller.config import settings


class ProvisionError(RuntimeError):
    """Raised when fleet cannot mint/remove a cert (transport or server error)."""


def _require_config() -> None:
    if not settings.fleet_url:
        raise ProvisionError("NFF_MOCKCTL_FLEET_URL is not configured")
    if not settings.fleet_secret:
        raise ProvisionError("NFF_MOCKCTL_FLEET_SECRET is not configured")


async def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    _require_config()
    url = settings.fleet_url.rstrip("/") + path
    async with httpx.AsyncClient(timeout=settings.fleet_timeout) as client:
        try:
            resp = await client.post(
                url, json=body, headers={"X-Fleet-Secret": settings.fleet_secret}
            )
        except httpx.HTTPError as exc:
            raise ProvisionError(f"fleet request failed: {exc}") from exc

    try:
        data = resp.json()
    except ValueError:
        data = {"error": resp.text}
    if resp.status_code != 200:
        raise ProvisionError(f"fleet returned {resp.status_code}: {data.get('error', data)}")
    return data


async def provision(device_id: str, project_id: str) -> dict[str, Any]:
    """Mint a project-bound client cert for ``device_id`` (fleet writes /data/certs/<id>/
    and ensures the project's command-signing key). Fleet requires project_id."""
    return await _post("/internal/provision", {"device_id": device_id, "project_id": project_id})


async def deprovision(device_id: str) -> dict[str, Any]:
    """Remove ``device_id``'s certs. Fleet rejects non-mock-prefixed ids (403)."""
    return await _post("/internal/deprovision", {"device_id": device_id})
