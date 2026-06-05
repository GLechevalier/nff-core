"""Internal service-to-service control surface.

nff-fleet is a *command sender*: it owns the MQTT broker, the signing key, and the
device channel. It does NOT decide anything about OTA — that is nff-ota's job. When
nff-ota decides to push an OTA (or any command) to a device, it calls this endpoint
and nff-fleet signs the payload and delivers it over MQTT, returning the device's
response.

POST /internal/command
    Header: X-Fleet-Secret: <shared secret>   (settings.mcp_secret)
    Body:   {"device_id": "<mqtt identity>", "payload": {...}, "timeout": <float?>}
    -> 200  {"device_id": ..., "response": <device json>}
       401  bad/missing secret
       400  malformed body
       504  device did not respond in time
       500  delivery error

The payload's "action" drives signing generically (see signing.sign / _build_tbs),
so no OTA-specific code lives here.
"""

from __future__ import annotations

import hmac
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from nff_fleet.commands import send_command
from nff_fleet.config import settings


def _secret_ok(request: Request) -> bool:
    provided = request.headers.get("x-fleet-secret", "")
    return bool(provided) and hmac.compare_digest(provided, settings.mcp_secret)


async def _deliver_command(request: Request) -> JSONResponse:
    if not _secret_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    device_id = body.get("device_id")
    payload = body.get("payload")
    if not isinstance(device_id, str) or not device_id:
        return JSONResponse({"error": "device_id is required"}, status_code=400)
    if not isinstance(payload, dict) or "action" not in payload:
        return JSONResponse(
            {"error": "payload must be an object containing 'action'"}, status_code=400
        )

    timeout = body.get("timeout")
    if timeout is not None and not isinstance(timeout, (int, float)):
        return JSONResponse({"error": "timeout must be a number"}, status_code=400)

    try:
        resp = await send_command(
            device_id,
            payload,
            timeout=float(timeout) if timeout is not None else None,
            session_id="nff-ota",
        )
    except TimeoutError as exc:
        return JSONResponse(
            {"device_id": device_id, "error": str(exc)}, status_code=504
        )
    except Exception as exc:  # delivery/broker error
        return JSONResponse(
            {"device_id": device_id, "error": str(exc)}, status_code=500
        )

    return JSONResponse({"device_id": device_id, "response": resp})


def register(mcp) -> None:
    """Register the internal control routes on the FastMCP HTTP app."""
    mcp.custom_route("/internal/command", methods=["POST"])(_deliver_command)
