"""Client for the nff-fleet command-delivery plane.

nff-ota is the OTA brain — it decides what firmware goes to which device and when.
It does NOT talk to devices directly. The only wire to a device is nff-fleet's MQTT
broker, so nff-ota asks fleet to deliver a (signed) command via fleet's internal
HTTP endpoint:

    POST {fleet_url}/internal/command
        Header: X-Fleet-Secret: <shared secret>
        Body:   {"device_id": "<mqtt id>", "project_id": "<id>", "payload": {...}, "timeout": <float>}

Fleet signs the payload (its signing key + the device's OTA to-be-signed format live
there), publishes it over MQTT, awaits the device's response, and returns it.
"""

from __future__ import annotations

from typing import Any

import httpx

from nff_ota.config import settings


class FleetError(RuntimeError):
    """Raised when nff-fleet cannot deliver a command (transport or device error)."""


def _require_config() -> None:
    if not settings.fleet_url:
        raise FleetError("OTA_FLEET_URL is not configured — cannot deliver commands")
    if not settings.fleet_secret:
        raise FleetError("OTA_FLEET_SECRET is not configured — cannot authenticate to fleet")


async def send_command(
    mqtt_id: str,
    payload: dict[str, Any],
    project_id: str,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Ask nff-fleet to sign + deliver `payload` to the device identified by `mqtt_id`.

    `project_id` scopes the command: fleet signs with the device's project key and publishes
    to the project-namespaced topic, so a command for the wrong project won't verify on the
    device. Returns the device's response JSON. Raises FleetError on auth/transport/device
    failure (including a device timeout, which fleet reports as HTTP 504).
    """
    _require_config()
    request_timeout = timeout if timeout is not None else settings.fleet_timeout
    url = settings.fleet_url.rstrip("/") + "/internal/command"
    body = {
        "device_id": mqtt_id,
        "project_id": project_id,
        "payload": payload,
        "timeout": request_timeout,
    }

    # Allow extra wall-clock beyond the device timeout for the round trip itself.
    async with httpx.AsyncClient(timeout=request_timeout + 30.0) as client:
        try:
            resp = await client.post(
                url, json=body, headers={"X-Fleet-Secret": settings.fleet_secret}
            )
        except httpx.HTTPError as exc:
            raise FleetError(f"fleet request failed: {exc}") from exc

    try:
        data = resp.json()
    except ValueError:
        data = {"error": resp.text}

    if resp.status_code != 200:
        raise FleetError(
            f"fleet returned {resp.status_code}: {data.get('error', data)}"
        )
    return data.get("response", {})


async def deliver_ota(
    mqtt_id: str,
    version: str,
    sha256: str,
    url: str,
    project_id: str,
    timeout: float | None = None,
    build_id: str | None = None,
) -> dict[str, Any]:
    """Deliver an OTA command for one device through nff-fleet.

    `version`/`sha256`/`url` come from nff-ota's own artifact record; `project_id` is the
    device's tenant scope. Fleet builds the `ota|version|sha256|nonce|timestamp` signature
    with the project's key and pushes it over MQTT.

    `build_id` (the per-commit id baked into the binary) is sent as an unsigned extra
    field — it is NOT part of the OTA signature TBS. Real firmware ignores it (it reports
    its own compiled-in build id); a software mock, which can't run the binary, adopts it
    so its heartbeat build_id matches what was deployed and post-deploy verification passes.
    """
    payload = {"action": "ota", "version": version, "sha256": sha256, "url": url}
    if build_id:
        payload["build_id"] = build_id
    return await send_command(mqtt_id, payload, project_id, timeout=timeout)
