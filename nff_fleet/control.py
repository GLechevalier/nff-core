"""Internal service-to-service control surface.

nff-fleet is a *command sender*: it owns the MQTT broker, the signing key, and the
device channel. It does NOT decide anything about OTA — that is nff-ota's job. When
nff-ota decides to push an OTA (or any command) to a device, it calls this endpoint
and nff-fleet signs the payload and delivers it over MQTT, returning the device's
response.

POST /internal/command
    Header: X-Fleet-Secret: <shared secret>   (settings.mcp_secret)
    Body:   {"device_id": "<id>", "project_id": "<id>", "payload": {...}, "timeout": <float?>}
    -> 200  {"device_id": ..., "response": <device json>}
       401  bad/missing secret
       400  malformed body
       504  device did not respond in time
       500  delivery error

The payload's "action" drives signing generically (see signing.sign / _build_tbs); the
command is signed with the device's *project* key, so the caller must pass project_id.

POST /internal/provision
    Header: X-Fleet-Secret: <shared secret>
    Body:   {"device_id": "<id>", "project_id": "<id>"}
    Mint a per-device mTLS client cert bound to project_id (signed cert extension) and
    ensure the project's command-signing key exists.
POST /internal/deprovision
    Header: X-Fleet-Secret: <shared secret>
    Body:   {"device_id": "<id>"}
    Remove a device's credentials. The CA *signing key* never leaves fleet — public-facing
    callers (nff-mock-controller) ask fleet to issue certs rather than holding the CA key
    themselves. deprovision is constrained to mock-prefixed ids so it can never delete a
    real device's credentials.

POST /internal/store-elf
    Header: X-Fleet-Secret: <shared secret>
    Body:   {"build_id": "<16hex>", "elf_b64": "<base64 ELF>"}
    Stage a build's ELF for crash symbolication, keyed by build_id (the HTTP analogue of the
    fleet_store_elf MCP tool). nff-ota's Stage-4 L2 compile path posts the freshly-built ELF
    so fleet_resolve_backtrace can resolve a QEMU crash's backtrace.
"""

from __future__ import annotations

import hmac
import logging
import shutil
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from nff_fleet import db
from nff_fleet.commands import send_command
from nff_fleet.config import settings

logger = logging.getLogger(__name__)

# The mock controller spawns ids like "mock-<project8>-<rand6>"; only these may be
# deprovisioned over HTTP, so a bug/abuse can never wipe a real device's certs.
_MOCK_ID_PREFIX = "mock-"


def _secret_ok(request: Request) -> bool:
    provided = request.headers.get("x-fleet-secret", "")
    return bool(provided) and hmac.compare_digest(provided, settings.mcp_secret)


def _valid_device_id(device_id: Any) -> bool:
    """A device_id must be a non-empty str usable as a single path segment."""
    return (
        isinstance(device_id, str)
        and bool(device_id)
        and "/" not in device_id
        and "\\" not in device_id
        and device_id not in (".", "..")
    )


async def _deliver_command(request: Request) -> JSONResponse:
    if not _secret_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    device_id = body.get("device_id")
    payload = body.get("payload")
    project_id = body.get("project_id")
    if not isinstance(device_id, str) or not device_id:
        return JSONResponse({"error": "device_id is required"}, status_code=400)
    if not _valid_device_id(project_id):
        return JSONResponse({"error": "valid project_id is required"}, status_code=400)
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
            project_id,
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


async def _provision_device(request: Request) -> JSONResponse:
    if not _secret_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    device_id = body.get("device_id")
    project_id = body.get("project_id")
    if not _valid_device_id(device_id):
        return JSONResponse({"error": "valid device_id is required"}, status_code=400)
    if not _valid_device_id(project_id):
        return JSONResponse({"error": "valid project_id is required"}, status_code=400)

    try:
        from nff_fleet.provisioning.ca import issue_device_cert
        from nff_fleet import signing

        # Ownership is recorded at issue time: the cert binds device → project, and the
        # project's command-signing key is created on first provision into it.
        signing.ensure_project_key(project_id)
        cert_path, key_path = issue_device_cert(device_id, project_id)
    except Exception as exc:  # CA not initialised, write error, etc.
        return JSONResponse(
            {"device_id": device_id, "error": str(exc)}, status_code=500
        )

    return JSONResponse(
        {
            "device_id": device_id,
            "cert_path": str(cert_path),
            "key_path": str(key_path),
        }
    )


async def _provision_batch(request: Request) -> JSONResponse:
    """Create an enrollment batch + its shared bootstrap credential (DEVICE_OWNERSHIP_DESIGN.md §8).

    POST /internal/provision-batch
        Header: X-Fleet-Secret
        Body:   {"project_id": "<uuid>", "count": <int?>}
        -> 200  {"batch_id", "project_id", "expires_in_hours", "bootstrap_header_path"}
    Called by `nff provision batch`. Mints ONE shared cert for the whole batch and records the
    batch (with its 24h window + optional quota) so devices can announce + be claimed.
    """
    if not _secret_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    project_id = body.get("project_id")
    count = body.get("count")
    if not _valid_device_id(project_id):
        return JSONResponse({"error": "valid project_id is required"}, status_code=400)
    if count is not None and (not isinstance(count, int) or count <= 0):
        return JSONResponse({"error": "count must be a positive integer"}, status_code=400)

    try:
        from nff_fleet.provisioning import batch as batch_mod
        from nff_fleet.provisioning.credentials import render_batch_bootstrap_h, write_batch_bootstrap_h
        from nff_fleet import signing

        # The project must have its intermediate CA + signing key before any device claims into it.
        signing.ensure_project_key(project_id)
        # Idempotency: reuse a still-valid active batch for this project so repeat
        # onboarding runs don't pile up batches + certs. The leaf already exists for a
        # reused batch, so render the header from stored material without re-issuing.
        batch_id = await db.get_active_batch(project_id)
        reused = batch_id is not None
        if not batch_id:
            batch_id = await db.create_enrollment_batch(project_id, count, None)
            if not batch_id:
                return JSONResponse(
                    {"error": "persistence disabled — cannot create batch"}, status_code=503
                )
            batch_mod.issue_batch_cert(project_id, batch_id)
        header_path = write_batch_bootstrap_h(project_id, batch_id)
        # Return the rendered header inline so a remote `nff provision batch` can write it locally
        # (the path is only meaningful on a co-located deployment sharing /data).
        header_content = render_batch_bootstrap_h(project_id, batch_id)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse(
        {
            "batch_id": batch_id,
            "project_id": project_id,
            "expires_in_hours": 24,
            "reused": reused,
            "bootstrap_header_path": str(header_path),
            "bootstrap_header": header_content,
        }
    )


async def _deprovision_device(request: Request) -> JSONResponse:
    if not _secret_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    device_id = body.get("device_id")
    if not _valid_device_id(device_id):
        return JSONResponse({"error": "valid device_id is required"}, status_code=400)
    if not device_id.startswith(_MOCK_ID_PREFIX):
        # Refuse to touch anything but mock-spawned identities.
        return JSONResponse(
            {"error": f"deprovision is restricted to '{_MOCK_ID_PREFIX}' ids"},
            status_code=403,
        )

    device_dir = settings.certs_dir / device_id
    try:
        shutil.rmtree(device_dir, ignore_errors=True)
    except Exception as exc:
        return JSONResponse(
            {"device_id": device_id, "error": str(exc)}, status_code=500
        )

    # Also drop the dashboard device row (telemetry/crash cascade) so a reaped mock
    # disappears instead of lingering as an offline ghost. Best-effort: cert removal
    # is the contract; a leftover row just shows offline. RPC is mock-id-guarded.
    try:
        await db.delete_mock_device(device_id)
    except Exception as exc:
        logger.warning("deprovision %s: device-row cleanup failed: %s", device_id, exc)

    return JSONResponse({"device_id": device_id, "removed": True})


async def _chaos_reset(request: Request) -> JSONResponse:
    """Test-only: zero the chaos counter so the next NFF_FLEET_CHAOS_FAIL_FIRST pings fail
    again. Lets the e2e transient track (Track 3) re-arm per iteration without a fleet restart.
    Harmless no-op when chaos is unarmed (env 0) — the env is still the only switch."""
    if not _secret_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from nff_fleet.tools.devices import reset_chaos

    fail_first = reset_chaos()
    return JSONResponse({"reset": True, "fail_first": fail_first})


async def _store_elf(request: Request) -> JSONResponse:
    """Stage a build's ELF for crash symbolication, keyed by an explicit build_id.

    The HTTP analogue of the fleet_store_elf MCP tool: nff-ota's Stage-4 L2 compile path posts
    the freshly-built ELF here so fleet_resolve_backtrace can resolve a QEMU crash's backtrace
    (the device reports the same build_id, baked in as NFF_BUILD_ID). Body:
    {"build_id": "<16hex>", "elf_b64": "<base64 ELF>"}.
    """
    if not _secret_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "malformed body"}, status_code=400)

    build_id = body.get("build_id")
    elf_b64 = body.get("elf_b64")
    if not _valid_device_id(build_id):  # same single-path-segment safety as a device id
        return JSONResponse({"error": "build_id is required and must be a path segment"}, status_code=400)
    if not isinstance(elf_b64, str) or not elf_b64:
        return JSONResponse({"error": "elf_b64 is required"}, status_code=400)

    from nff_fleet.tools.ota import fleet_store_elf

    try:
        result = await fleet_store_elf(build_id, elf_b64)
    except Exception as exc:  # noqa: BLE001 — bad base64 / write failure
        logger.warning("store-elf failed for %s: %s", build_id, exc)
        return JSONResponse({"error": f"could not store ELF: {exc}"}, status_code=500)
    return JSONResponse(result)


async def _store_bin(request: Request) -> JSONResponse:
    """Stage a build's merged flash image, keyed by an explicit build_id.

    The L2 supervisor reads this back off the shared /data mount to seed a fresh emulated device's
    flash. nff-ota's Stage-4 L2 compile path posts the freshly-built merged.bin here beside the ELF.
    Body: {"build_id": "<16hex>", "bin_b64": "<base64 merged.bin>"}.
    """
    if not _secret_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "malformed body"}, status_code=400)

    build_id = body.get("build_id")
    bin_b64 = body.get("bin_b64")
    if not _valid_device_id(build_id):  # same single-path-segment safety as a device id
        return JSONResponse({"error": "build_id is required and must be a path segment"}, status_code=400)
    if not isinstance(bin_b64, str) or not bin_b64:
        return JSONResponse({"error": "bin_b64 is required"}, status_code=400)

    from nff_fleet.tools.ota import fleet_store_bin

    try:
        result = await fleet_store_bin(build_id, bin_b64)
    except Exception as exc:  # noqa: BLE001 — bad base64 / write failure
        logger.warning("store-bin failed for %s: %s", build_id, exc)
        return JSONResponse({"error": f"could not store bin: {exc}"}, status_code=500)
    return JSONResponse(result)


def register(mcp) -> None:
    """Register the internal control routes on the FastMCP HTTP app."""
    mcp.custom_route("/internal/command", methods=["POST"])(_deliver_command)
    mcp.custom_route("/internal/provision", methods=["POST"])(_provision_device)
    mcp.custom_route("/internal/provision-batch", methods=["POST"])(_provision_batch)
    mcp.custom_route("/internal/deprovision", methods=["POST"])(_deprovision_device)
    mcp.custom_route("/internal/chaos-reset", methods=["POST"])(_chaos_reset)
    mcp.custom_route("/internal/store-elf", methods=["POST"])(_store_elf)
    mcp.custom_route("/internal/store-bin", methods=["POST"])(_store_bin)
