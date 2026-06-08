"""Inbound HTTP webhooks for nff-ota.

nff-ota normally only makes OUTBOUND calls to nff-fleet (see ``fleet.py``). This
module adds the one inbound endpoint that closes the OTA feedback loop: when a
device finishes an update and reboots, the device publishes an ``ota_result`` over
MQTT, nff-fleet's router forwards it here, and we finalize the per-device job.

    POST /internal/ota-result
        Header: X-Fleet-Secret: <shared secret>   (settings.fleet_secret == fleet's mcp_secret)
        Body:   {"device_id": "<id>", "status": "committed"|"rolled_back", "version": "<v>"}

Mirrors nff-fleet's ``control.py`` shared-secret pattern.
"""

from __future__ import annotations

import hmac

from starlette.requests import Request
from starlette.responses import JSONResponse

from nff_ota.config import settings
from nff_ota.tools.deployments.ops import finalize_ota_result


def _secret_ok(request: Request) -> bool:
    # Require a configured secret — an unset secret must never authenticate, otherwise
    # an empty header would compare-equal to an empty configured value.
    if not settings.fleet_secret:
        return False
    provided = request.headers.get("x-fleet-secret", "")
    return bool(provided) and hmac.compare_digest(provided, settings.fleet_secret)


async def _ota_result(request: Request) -> JSONResponse:
    if not _secret_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    device_id = body.get("device_id")
    status = body.get("status")
    version = body.get("version")
    if not isinstance(device_id, str) or not device_id:
        return JSONResponse({"error": "device_id is required"}, status_code=400)
    if status not in ("committed", "rolled_back"):
        return JSONResponse({"error": "status must be 'committed' or 'rolled_back'"}, status_code=400)
    if not isinstance(version, str) or not version:
        return JSONResponse({"error": "version is required"}, status_code=400)

    result = await finalize_ota_result(device_id, status, version)
    return JSONResponse(result)


def register(root) -> None:
    """Register inbound HTTP routes on the FastMCP HTTP app."""
    root.custom_route("/internal/ota-result", methods=["POST"])(_ota_result)
