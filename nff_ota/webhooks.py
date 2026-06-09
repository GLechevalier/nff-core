"""Inbound HTTP webhooks for nff-ota.

nff-ota normally only makes OUTBOUND calls to nff-fleet (see ``fleet.py``). This
module adds the inbound endpoints that other trusted services call:

    POST /internal/ota-result
        Header: X-Fleet-Secret: <shared secret>   (settings.fleet_secret == fleet's mcp_secret)
        Body:   {"device_id": "<id>", "status": "committed"|"rolled_back", "version": "<v>"}
        Closes the OTA feedback loop: when a device finishes an update and reboots it
        publishes an ``ota_result`` over MQTT, nff-fleet's router forwards it here, and
        we finalize the per-device job.

    POST /internal/build-deploy
        Header: X-Fleet-Secret: <shared secret>
        Body:   {version, name, device_types[], (artifact_b64 | file_path),
                 (device_group_id | device_group_name), max_devices_in_flight?, retries?,
                 build_id?, elf_b64?, branch_name?, author?, committed_at?, changelog?,
                 parent_version?}
        One-call "ship this freshly built binary" entrypoint used by the nff-github
        connector: uploads the artifact (+ optional debug ELF and git lineage), then
        creates and starts a deployment to the target group. Wraps the existing
        upload_artifact / create_deployment / start_deployment ops so nff-ota stays the
        sole owner of deploy logic.

Mirrors nff-fleet's ``control.py`` shared-secret pattern.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import tempfile
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from nff_ota.client import ensure_client, get_client
from nff_ota.config import settings
from nff_ota.models.artifact import UploadArtifactParams
from nff_ota.storage import upload_elf
from nff_ota.models.deployment import CreateDeploymentParams
from nff_ota.tools.artifacts.ops import upload_artifact
from nff_ota.tools.deployments.ops import (
    create_deployment,
    finalize_ota_result,
    group_member_device_types,
    start_deployment,
)


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

    # Webhook routes bypass the per-session MCP lifespan, so the Supabase client may not
    # be initialised yet; ensure it before any downstream get_client() call.
    await ensure_client()

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


async def _resolve_group_id(body: dict) -> tuple[str | None, str | None]:
    """Resolve the deployment target group id from the request body.

    Accepts an explicit ``device_group_id`` or a ``device_group_name`` to look up.
    Returns (group_id, error); group membership/enrollment is a deliberate step that
    stays out of this path, so an unknown name is an error rather than an auto-create.
    """
    group_id = body.get("device_group_id")
    if isinstance(group_id, str) and group_id:
        return group_id, None

    name = body.get("device_group_name")
    if isinstance(name, str) and name:
        client = get_client()
        resp = await client.table("device_groups").select("id").eq("name", name).execute()
        if not resp.data:
            return None, f"device_group_name {name!r} not found"
        return resp.data[0]["id"], None

    return None, "device_group_id or device_group_name is required"


async def _build_deploy(request: Request) -> JSONResponse:
    if not _secret_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # Webhook routes bypass the per-session MCP lifespan, so the Supabase client may not
    # be initialised yet; ensure it before any downstream get_client() call.
    await ensure_client()

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    version = body.get("version")
    name = body.get("name")
    device_types = body.get("device_types")
    if not isinstance(version, str) or not version:
        return JSONResponse({"error": "version is required"}, status_code=400)
    if not isinstance(name, str) or not name:
        return JSONResponse({"error": "name is required"}, status_code=400)

    group_id, group_err = await _resolve_group_id(body)
    if group_err:
        return JSONResponse({"error": group_err}, status_code=400)

    # device_types is the firmware's target allow-list. A caller may pass it explicitly,
    # but the target group is the source of truth for what hardware is actually in play:
    # when the caller didn't supply a usable list, derive it from the self-attested
    # device_type of the group's members instead of forcing static config.
    if not (isinstance(device_types, list) and device_types):
        device_types = await group_member_device_types(group_id)
    if not device_types:
        return JSONResponse(
            {"error": "device_types must be a non-empty list, and none could be "
                      "derived from the target group's devices (no member has "
                      "reported a device_type yet)"},
            status_code=400,
        )

    # The binary arrives either inline (base64, host-agnostic) or as a path the caller
    # and nff-ota both see (co-located). Inline bytes are written to a temp file because
    # upload_artifact reads from a path.
    tmp_path: Path | None = None
    file_path = body.get("file_path")
    artifact_b64 = body.get("artifact_b64")
    if isinstance(artifact_b64, str) and artifact_b64:
        try:
            data = base64.b64decode(artifact_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            return JSONResponse({"error": f"artifact_b64 is not valid base64: {e}"}, status_code=400)
        fd = tempfile.NamedTemporaryFile(prefix="nff-build-", suffix=".bin", delete=False)
        try:
            fd.write(data)
        finally:
            fd.close()
        tmp_path = Path(fd.name)
        file_path = str(tmp_path)
    elif not (isinstance(file_path, str) and file_path):
        return JSONResponse({"error": "artifact_b64 or file_path is required"}, status_code=400)

    # Optional debug ELF (base64): stored alongside the .bin for crash symbolication and
    # never delivered to a device. A bad base64 blob is non-fatal — the deploy proceeds
    # without the ELF rather than failing the whole rollout over a diagnostics extra.
    elf_path: str | None = None
    elf_b64 = body.get("elf_b64")
    if isinstance(elf_b64, str) and elf_b64:
        try:
            elf_path = await upload_elf(version, base64.b64decode(elf_b64, validate=True))
        except (binascii.Error, ValueError) as e:
            return JSONResponse({"error": f"elf_b64 is not valid base64: {e}"}, status_code=400)
        except Exception:  # storage hiccup on the ELF must not abort the firmware rollout
            elf_path = None

    def _str(key: str) -> str | None:
        val = body.get(key)
        return val if isinstance(val, str) and val else None

    try:
        upload = await upload_artifact(
            UploadArtifactParams(
                name=name, version=version, device_types=device_types, file_path=file_path,
                build_id=_str("build_id"),
                branch_name=_str("branch_name"),
                author=_str("author"),
                committed_at=_str("committed_at"),
                changelog=body.get("changelog") if isinstance(body.get("changelog"), str) else None,
                parent_version=_str("parent_version"),
                elf_path=elf_path,
                release_version=_str("release_version"),
            )
        )
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    if upload.error:
        return JSONResponse({"error": f"upload failed: {upload.error}"}, status_code=400)

    create_kwargs: dict = {
        "name": name,
        "target_version": version,
        "device_group_id": group_id,
    }
    if isinstance(body.get("max_devices_in_flight"), int):
        create_kwargs["max_devices_in_flight"] = body["max_devices_in_flight"]
    if isinstance(body.get("retries"), int):
        create_kwargs["retries"] = body["retries"]

    created = await create_deployment(CreateDeploymentParams(**create_kwargs))
    if created.error or created.deployment is None:
        return JSONResponse({"error": f"create_deployment failed: {created.error}"}, status_code=400)

    deployment_id = created.deployment.id
    started = await start_deployment(deployment_id)

    return JSONResponse(
        {
            "deployment_id": deployment_id,
            "version": version,
            "delivered": started.delivered,
            "failed": started.failed,
            "skipped": started.skipped,
            # start_deployment sets `error` for soft conditions (e.g. nothing pending);
            # surface it without failing the whole request.
            "start_error": started.error,
        }
    )


def register(root) -> None:
    """Register inbound HTTP routes on the FastMCP HTTP app."""
    root.custom_route("/internal/ota-result", methods=["POST"])(_ota_result)
    root.custom_route("/internal/build-deploy", methods=["POST"])(_build_deploy)
