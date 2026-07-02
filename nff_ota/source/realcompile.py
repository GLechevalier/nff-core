"""Real server-side compile + deploy for L2 (nff-qemu-esp32) devices — Stage 4.

The L1 path (``source/ops.py::build_and_deploy_source``) "compiles" a fictional pseudo-.ino with
a regex analyzer and bakes a *simulated* fault into a JSON sketch. An L2 device runs a real ESP32
firmware image in ``qemu-system-xtensa``, so its firmware must be **genuinely compiled** from the
submitted source. This module is the qemu branch: it asks nff-github to compile the source for real
(``POST /internal/compile-source`` → a merged flash image + debug ELF), rolls the merged image out
through the EXISTING OTA path (``upload_artifact`` → ``create_deployment`` → ``start_deployment``),
and stages the ELF on nff-fleet keyed by ``build_id`` so a QEMU crash's backtrace resolves.

There is no compile-time ``fault`` for L2 — the firmware either runs or genuinely panics in QEMU,
which is the whole point (it fixes the byte-identical/fake-crash flaw). So ``fault`` is recorded as
None and ``fault_remaining`` is always False.
"""

from __future__ import annotations

import base64
import logging
import tempfile
import uuid
from pathlib import Path

from nff_ota import fleet, github
from nff_ota.client import get_client
from nff_ota.models.artifact import UploadArtifactParams
from nff_ota.models.deployment import CreateDeploymentParams
from nff_ota.models.source import SubmitFirmwareFixResult
from nff_ota.source.compile import build_id_for
from nff_ota.source.ops import _catalog_version
from nff_ota.tools.artifacts.ops import upload_artifact
from nff_ota.tools.deployments.ops import create_deployment, start_deployment

logger = logging.getLogger(__name__)


async def real_build_and_deploy(
    device_id: str,
    version: str,
    source: str,
    author: str,
    *,
    project_id: str,
    group_id: str,
    device_type: str,
    parent_version: str | None = None,
    changelog: str | None = None,
) -> SubmitFirmwareFixResult:
    """Compile ``source`` for real via nff-github, store the ELF on fleet, and roll it out.

    Caller (``build_and_deploy_source``) has already resolved ``project_id`` / ``group_id`` /
    ``device_type`` and gated the semver, so this focuses on the real-compile mechanics."""
    client = get_client()
    build_id = build_id_for(source)  # identical contract to L1: sha256(source)[:16]

    # 1. Real compile (the regex analyzer's L2 replacement).
    try:
        result = await github.compile_source(source, build_id, version, device_type)
    except github.GithubError as exc:
        return SubmitFirmwareFixResult(error=f"real compile failed: {exc}")

    # 2. Record the revision (the source of truth the agent reads back). No compile-time fault
    #    for L2 — a genuine panic happens at runtime in QEMU, not via a regex match.
    try:
        await client.table("mock_firmware_revisions").insert(
            {
                "id": str(uuid.uuid4()),
                "project_id": project_id,
                "device_id": device_id,
                "version": version,
                "source": source,
                "fault": None,
                "build_id": build_id,
                "parent_version": parent_version,
                "author": author,
            }
        ).execute()
    except Exception as exc:  # noqa: BLE001 — unique(device_id, version) or RLS/db error
        return SubmitFirmwareFixResult(error=f"could not record revision: {exc}")

    # 3. Upload the merged flash image as the deployable artifact.
    catalog_version = _catalog_version(device_id, version)
    bin_bytes = base64.b64decode(result["artifact_b64"])
    fd = tempfile.NamedTemporaryFile(prefix="nff-l2-fw-", suffix=".bin", delete=False)
    try:
        fd.write(bin_bytes)
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

    # 4. Stage the ELF on fleet under build_id so fleet_resolve_backtrace can symbolicate the
    #    QEMU crash. A missing ELF must not block the rollout — it only costs us a resolved
    #    backtrace, not the firmware.
    elf_b64 = result.get("elf_b64")
    if elf_b64:
        try:
            await fleet.store_elf(build_id, elf_b64)
        except fleet.FleetError as exc:
            logger.warning("could not store ELF for %s (build_id=%s): %s", device_id, build_id, exc)
    else:
        logger.warning("real compile for %s returned no ELF — backtraces will not resolve", device_id)

    # 4b. Stage the merged flash image on fleet under the same build_id, so the L2 supervisor can
    #    seed a fresh emulated device's flash by reading it off the shared /data mount
    #    (NFF_MOCK_FIRMWARE_IMAGE = /data/firmware/{build_id}/firmware.bin). Best-effort — the
    #    device still gets this build via the OTA path below; store_bin only enables cold-boot
    #    spawns of a device already pinned to this build_id.
    try:
        await fleet.store_bin(build_id, result["artifact_b64"])
    except fleet.FleetError as exc:
        logger.warning("could not store bin for %s (build_id=%s): %s", device_id, build_id, exc)

    # 5. Deploy to the device's group via the normal OTA path.
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
        "L2 deployed %s v%s build_id=%s (delivered=%s)",
        device_id, version, build_id, started.delivered,
    )
    return SubmitFirmwareFixResult(
        deployment_id=deployment_id, version=version, fault_remaining=False
    )
