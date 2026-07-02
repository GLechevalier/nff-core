"""Store + build + deploy for the self-contained virtual firmware store.

One core helper, ``build_and_deploy_source``, is shared by the harness seed
(``/internal/firmware/seed``) and the agent fix (``submit_firmware_fix``): it compiles the
source, records a revision in ``mock_firmware_revisions``, uploads the compiled sketch as the
deployable artifact, and rolls it out to the device's group through the EXISTING OTA path
(``upload_artifact`` → ``create_deployment`` → ``start_deployment``). The mock downloads the
sketch, adopts it, and crash-loops iff the held source is buggy.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
import uuid
from pathlib import Path

from nff_ota.client import get_client
from nff_ota.models.artifact import UploadArtifactParams
from nff_ota.models.deployment import CreateDeploymentParams
from nff_ota.models.source import (
    FirmwareSourceInfo,
    GetFirmwareSourceResult,
    SubmitFirmwareFixResult,
)
from nff_ota.source.compile import compile_sketch
from nff_ota.tools.artifacts.ops import upload_artifact
from nff_ota.tools.deployments.ops import create_deployment, start_deployment

logger = logging.getLogger(__name__)

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

# An L2 device runs a real ESP32 firmware image in qemu-system-xtensa, so its firmware is compiled
# for real (nff-github) instead of L1's regex analyzer — see source/realcompile.py.
QEMU_DEVICE_TYPE = "nff-qemu-esp32"


def _semver(s: str | None) -> tuple[int, int, int] | None:
    m = _SEMVER.match((s or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _catalog_version(device_id: str, version: str) -> str:
    """The firmware_versions PK for a revision. Namespaced by device so two mocks can each
    ship their own line at the same semver without colliding on the shared catalog. The
    on-the-wire version is still the bare semver (release_version), which the device gates on."""
    return f"{device_id}-{version}"


async def _latest_revision(device_id: str) -> dict | None:
    """The most recently shipped revision for a device — the source it currently holds."""
    client = get_client()
    resp = (
        await client.table("mock_firmware_revisions")
        .select("*")
        .eq("device_id", device_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return resp.data[0] if resp.data else None


async def _device_group_id(device_id: str) -> str | None:
    """The device's first group membership (the deployment target). The mock is enrolled +
    grouped out-of-band (the seed step via /internal/ensure-mock-target)."""
    client = get_client()
    resp = (
        await client.table("device_group_members")
        .select("group_id")
        .eq("device_id", device_id)
        .limit(1)
        .execute()
    )
    return resp.data[0]["group_id"] if resp.data else None


async def build_and_deploy_source(
    device_id: str,
    version: str,
    source: str,
    author: str,
    *,
    parent_version: str | None = None,
    changelog: str | None = None,
) -> SubmitFirmwareFixResult:
    """Compile ``source``, record a revision, and roll it out to ``device_id``'s group.

    Returns a SubmitFirmwareFixResult carrying the deployment id and whether the compiled
    source still contains a fault (so a caller / the agent gets honest feedback)."""
    client = get_client()

    dev_resp = (
        await client.table("devices")
        .select("id, project_id, device_type")
        .eq("id", device_id)
        .execute()
    )
    if not dev_resp.data:
        return SubmitFirmwareFixResult(error=f"device {device_id!r} not found")
    project_id = dev_resp.data[0].get("project_id")
    if not project_id:
        return SubmitFirmwareFixResult(
            error=f"device {device_id!r} has no project_id (tenant) — re-provision it"
        )
    device_type = (dev_resp.data[0].get("device_type") or "esp32").strip() or "esp32"

    group_id = await _device_group_id(device_id)
    if not group_id:
        return SubmitFirmwareFixResult(
            error=f"device {device_id!r} is not in a device group — enroll it first"
        )

    # L2 (nff-qemu-esp32): compile the source for real and store the ELF so QEMU crash
    # backtraces resolve. Lazy import avoids a realcompile↔ops module cycle (realcompile
    # imports _catalog_version from here).
    if device_type == QEMU_DEVICE_TYPE:
        from nff_ota.source.realcompile import real_build_and_deploy

        return await real_build_and_deploy(
            device_id, version, source, author,
            project_id=project_id, group_id=group_id, device_type=device_type,
            parent_version=parent_version, changelog=changelog,
        )

    sketch, fault, build_id = compile_sketch(source, version=version)

    # 1. Record the revision (the source of truth the agent reads back).
    try:
        await client.table("mock_firmware_revisions").insert(
            {
                "id": str(uuid.uuid4()),
                "project_id": project_id,
                "device_id": device_id,
                "version": version,
                "source": source,
                "fault": fault,
                "build_id": build_id,
                "parent_version": parent_version,
                "author": author,
            }
        ).execute()
    except Exception as exc:  # noqa: BLE001 — unique(device_id, version) or RLS/db error
        return SubmitFirmwareFixResult(error=f"could not record revision: {exc}")

    # 2. Upload the compiled sketch as the deployable artifact (the mock downloads these bytes).
    catalog_version = _catalog_version(device_id, version)
    sketch_bytes = json.dumps(sketch).encode("utf-8")
    fd = tempfile.NamedTemporaryFile(prefix="nff-mock-fw-", suffix=".json", delete=False)
    try:
        fd.write(sketch_bytes)
        fd.close()
        upload = await upload_artifact(
            UploadArtifactParams(
                name=catalog_version,
                version=catalog_version,
                device_types=[device_type],
                file_path=fd.name,
                build_id=build_id,
                release_version=version,
                author=author,
                changelog=changelog,
            )
        )
    finally:
        Path(fd.name).unlink(missing_ok=True)
    if upload.error:
        return SubmitFirmwareFixResult(error=f"artifact upload failed: {upload.error}")

    # 3. Deploy to the device's group via the normal OTA path.
    created = await create_deployment(
        CreateDeploymentParams(
            name=f"{device_id} fw {version}",
            target_version=catalog_version,
            device_group_id=group_id,
        )
    )
    if created.error or created.deployment is None:
        return SubmitFirmwareFixResult(error=f"create_deployment failed: {created.error}")

    deployment_id = created.deployment.id
    started = await start_deployment(deployment_id)
    logger.info(
        "deployed %s v%s to %s (delivered=%s, fault=%s)",
        device_id, version, device_id, started.delivered, fault,
    )
    return SubmitFirmwareFixResult(
        deployment_id=deployment_id, version=version, fault_remaining=fault is not None
    )


async def get_firmware_source(device_id: str) -> GetFirmwareSourceResult:
    """Return the fictional firmware source the device currently holds."""
    rev = await _latest_revision(device_id)
    if not rev:
        return GetFirmwareSourceResult(
            error=f"no firmware source on record for device {device_id!r}"
        )
    return GetFirmwareSourceResult(
        source=FirmwareSourceInfo(
            device_id=device_id,
            version=rev["version"],
            source=rev["source"],
            build_id=rev["build_id"],
            fault=rev.get("fault"),
            author=rev.get("author") or "seed",
        )
    )


async def submit_firmware_fix(
    device_id: str, version: str, source: str, changelog: str | None = None
) -> SubmitFirmwareFixResult:
    """Ship a fixed firmware source as the next release and roll it out to the device.

    Enforces the device's anti-downgrade rule: ``version`` must be a bare semver strictly
    greater than the revision the device runs now."""
    new = _semver(version)
    if new is None:
        return SubmitFirmwareFixResult(
            error=f"version {version!r} must be a bare semver (major.minor.patch)"
        )
    current = await _latest_revision(device_id)
    if current is not None:
        cur = _semver(current.get("version"))
        if cur is not None and new <= cur:
            return SubmitFirmwareFixResult(
                error=(
                    f"version {version} is not strictly greater than the current "
                    f"{current['version']} — pick a higher semver"
                )
            )
    parent = current.get("version") if current else None
    return await build_and_deploy_source(
        device_id, version, source, author="agent",
        parent_version=parent, changelog=changelog,
    )
