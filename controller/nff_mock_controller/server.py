"""HTTP API + entrypoint for the mock controller.

A small Starlette app (run by uvicorn) that owns only spawn/list/destroy — ping, diag,
OTA, and crash go through the EXISTING fleet / nff-ota planes against the spawned mock,
so no control logic is duplicated here.

    POST   /mocks   {count?, project_id, ttl_s?, level?}          -> spawn N mocks
    GET    /mocks   ?project_id=<id>                              -> list mocks
    DELETE /mocks/{device_id}                                     -> tear one down
    GET    /health                                               -> liveness

    `level` (alias `tier`) picks the mock's fidelity: 0 = none (spawn nothing), 1 = the
    lightweight Python sim (default), 2 = the real firmware in QEMU, 3 = real hardware
    (501, not implemented). Per-level caps + image come from levels.profile_for().

All /mocks routes require the X-Control-Secret header (settings.control_secret).
"""

from __future__ import annotations

import hmac
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from nff_mock_controller import docker as dk
from nff_mock_controller import presence, provision, reaper
from nff_mock_controller.config import settings
from nff_mock_controller.levels import profile_for

logger = logging.getLogger(__name__)


def _secret_ok(request: Request) -> bool:
    provided = request.headers.get("x-control-secret", "")
    return bool(provided) and hmac.compare_digest(provided, settings.control_secret)


def _new_device_id(project_id: str | None) -> str:
    """mock-<project8|anon>-<rand6> — a valid container name, cert CN, and MQTT id."""
    proj8 = (project_id or "").replace("-", "")[:8] or "anon"
    return f"mock-{proj8}-{secrets.token_hex(3)}"


async def _create_mocks(request: Request) -> JSONResponse:
    if not _secret_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        body = {}

    try:
        count = int(body.get("count", 1))
    except (TypeError, ValueError):
        return JSONResponse({"error": "count must be an integer"}, status_code=400)
    if count < 1:
        return JSONResponse({"error": "count must be >= 1"}, status_code=400)

    # Complexity level (alias: tier). Default keeps today's behaviour (L1).
    raw_level = body.get("level", body.get("tier", settings.default_level))
    try:
        level = int(raw_level)
    except (TypeError, ValueError):
        return JSONResponse({"error": "level must be an integer"}, status_code=400)
    # Level 0 — spawn nothing (a way to ask for "no mocks" without special-casing callers).
    if level == 0:
        return JSONResponse({"spawned": []}, status_code=200)
    # Level 3 — real hardware. A recognised level with a fixed 501 (independent of
    # max_level, which only caps the *spawnable* tiers); surface the bridge URL if wired.
    if level == 3:
        return JSONResponse(
            {"error": "level 3 (real hardware) is not implemented",
             "hardware_bridge": settings.l3_bridge_url or None},
            status_code=501,
        )
    # Everything else must be a spawnable tier in [1, max_level].
    if level < 1 or level > settings.max_level:
        return JSONResponse(
            {"error": f"level must be between 0 and {settings.max_level}"}, status_code=400
        )
    profile = profile_for(level)

    # Fleet binds every device to a project (cert OID + per-project signing key) and
    # scopes topics by it, so a mock cannot exist without one.
    project_id = body.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        return JSONResponse({"error": "project_id is required"}, status_code=400)

    ttl_s = body.get("ttl_s") or profile.ttl_default_s
    try:
        ttl_s = int(ttl_s)
    except (TypeError, ValueError):
        return JSONResponse({"error": "ttl_s must be an integer"}, status_code=400)

    # Optional per-spawn overrides (used by the e2e suite): a buggy-version map
    # ("3.0.18=null_deref") and a starting firmware version. Empty/absent → fall back to
    # the controller-wide defaults inside docker.spawn().
    buggy_versions = body.get("buggy_versions")
    fw_version = body.get("fw_version")
    if buggy_versions is not None and not isinstance(buggy_versions, str):
        return JSONResponse({"error": "buggy_versions must be a string"}, status_code=400)
    if fw_version is not None and not isinstance(fw_version, str):
        return JSONResponse({"error": "fw_version must be a string"}, status_code=400)

    # Optional friendly display name (≠ device_id). With a prefix each mock gets a
    # distinct "<prefix> <n>" name (1-based); a single mock keeps the bare prefix.
    # Empty/absent → no name, so the device falls back to its device_id as before.
    name_prefix = body.get("name_prefix")
    if name_prefix is not None and not isinstance(name_prefix, str):
        return JSONResponse({"error": "name_prefix must be a string"}, status_code=400)
    name_prefix = (name_prefix or "").strip()

    # Cap enforcement against live state — counted per level, since L2 (heavy) has much
    # tighter caps than L1.
    try:
        existing = await dk.list_mocks()
    except dk.DockerError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)

    same_level = [m for m in existing if m.level == str(level)]
    if len(same_level) + count > profile.max_total:
        return JSONResponse(
            {"error": f"total cap reached for level {level} ({profile.max_total})"},
            status_code=429,
        )
    in_project = sum(1 for m in same_level if m.project_id == project_id)
    if in_project + count > profile.max_per_project:
        return JSONResponse(
            {"error": f"per-project cap reached for level {level} ({profile.max_per_project})"},
            status_code=429,
        )

    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_s)).isoformat()
    spawned: list[dict[str, str]] = []
    for i in range(count):
        device_id = _new_device_id(project_id)
        display_name = ""
        if name_prefix:
            display_name = f"{name_prefix} {i + 1}" if count > 1 else name_prefix
        try:
            await provision.provision(device_id, project_id)
        except provision.ProvisionError as exc:
            return JSONResponse(
                {"error": f"provision failed: {exc}", "spawned": spawned}, status_code=502
            )
        try:
            container_id = await dk.spawn(
                device_id, project_id, expires_at,
                profile=profile,
                buggy_versions=buggy_versions, fw_version=fw_version,
                display_name=display_name or None,
            )
        except dk.DockerError as exc:
            # Roll back the orphaned cert so a failed spawn leaves no residue.
            try:
                await provision.deprovision(device_id)
            except provision.ProvisionError:
                logger.warning("could not roll back cert for %s after spawn failure", device_id)
            return JSONResponse(
                {"error": f"spawn failed: {exc}", "spawned": spawned}, status_code=502
            )
        spawned.append({"device_id": device_id, "container_id": container_id[:12],
                        "expires_at": expires_at, "name": display_name or device_id,
                        "level": str(level)})

    return JSONResponse({"spawned": spawned}, status_code=201)


async def _list_mocks(request: Request) -> JSONResponse:
    if not _secret_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        mocks = await dk.list_mocks()
    except dk.DockerError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)

    project_id = request.query_params.get("project_id")
    if project_id:
        mocks = [m for m in mocks if m.project_id == project_id]
    return JSONResponse({"mocks": [m.__dict__ for m in mocks]})


async def _delete_mock(request: Request) -> JSONResponse:
    if not _secret_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    device_id = request.path_params["device_id"]
    if not device_id.startswith("mock-"):
        return JSONResponse(
            {"error": "only mock-prefixed ids can be deleted"}, status_code=403
        )
    try:
        await dk.kill_and_rm(dk.container_name(device_id))
    except dk.DockerError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    try:
        await provision.deprovision(device_id)
    except provision.ProvisionError as exc:
        logger.warning("deprovision %s failed: %s", device_id, exc)
    return JSONResponse({"device_id": device_id, "removed": True})


async def _presence(request: Request) -> JSONResponse:
    """Keep-alive heartbeat from a connected dashboard: mark a project as 'user online'.

    While a project's last ping is within ``presence_ttl_s`` the reaper never TTL-reaps its
    Level-1 mocks (see reaper._should_reap), so the fleet the user sees stays up as long as
    the tab is open and drains once the pings stop.
    """
    if not _secret_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        body = {}
    project_id = body.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        return JSONResponse({"error": "project_id is required"}, status_code=400)
    presence.mark_presence(project_id)
    return JSONResponse({"ok": True, "project_id": project_id, "ttl_s": settings.presence_ttl_s})


async def _renew_mock(request: Request) -> JSONResponse:
    """Reset a mock's idle lease — used to signal an L2 is being employed (compile/OTA/self-heal).

    Extends the device's deadline by ``ttl_s`` (default the L2 idle window). L1 mocks are governed
    by presence instead, so renewing one is a harmless no-op on their lifetime.
    """
    if not _secret_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        body = {}
    device_id = body.get("device_id")
    if not isinstance(device_id, str) or not device_id.startswith("mock-"):
        return JSONResponse({"error": "a 'mock-' device_id is required"}, status_code=400)
    ttl_s = body.get("ttl_s") or settings.l2_idle_ttl_s
    try:
        ttl_s = int(ttl_s)
    except (TypeError, ValueError):
        return JSONResponse({"error": "ttl_s must be an integer"}, status_code=400)
    if ttl_s < 1:
        return JSONResponse({"error": "ttl_s must be >= 1"}, status_code=400)
    deadline = presence.renew_device(device_id, ttl_s)
    return JSONResponse({"ok": True, "device_id": device_id, "expires_at": deadline.isoformat()})


async def _health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@asynccontextmanager
async def _lifespan(app: Starlette):
    # uvicorn keeps this loop alive for the server's lifetime, so the reaper ticks here.
    reaper.start()
    yield
    await reaper.stop()


app = Starlette(
    lifespan=_lifespan,
    routes=[
        Route("/health", _health, methods=["GET"]),
        Route("/mocks", _create_mocks, methods=["POST"]),
        Route("/mocks", _list_mocks, methods=["GET"]),
        # Keep-alive routes must precede the /{device_id} catch-all so "presence"/"renew" are
        # matched as literals, not treated as a device id.
        Route("/mocks/presence", _presence, methods=["POST"]),
        Route("/mocks/renew", _renew_mock, methods=["POST"]),
        Route("/mocks/{device_id}", _delete_mock, methods=["DELETE"]),
    ],
)


def main() -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(app, host=settings.http_host, port=settings.http_port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
