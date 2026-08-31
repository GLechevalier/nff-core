from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from nff_ota.client import get_client
from nff_ota.config import settings
from nff_ota.crypto import sha256_bytes, sign_ed25519, verify_ed25519
from nff_ota.models.artifact import ArtifactInfo, ArtifactResult, UploadArtifactParams
from nff_ota.storage import create_signed_url as _create_signed_url, delete_artifact_file, delete_elf, storage_path, upload_artifact as _upload_file

# Deployable-image columns nff-ota owns on the shared firmware_versions table.
_ARTIFACT_FIELDS = ("name", "device_types", "checksum_sha256", "size_bytes", "signature", "storage_path", "provides", "depends")

# Every ESP32 image (bootloader/app) starts with the ROM loader's magic byte.
_ESP_IMAGE_MAGIC = 0xE9


async def upload_artifact(params: UploadArtifactParams) -> ArtifactResult:
    """Upload a firmware artifact and register it against its version. The file is read from file_path on the server.

    A firmware version is the unit of identity (firmware_versions.version). Uploading
    attaches the deployable binary + checksum/signature to that version row, creating
    it if the version is not already tracked. Artifacts must look like real ESP32
    images (magic 0xE9, plausible size) — internal callers shipping to mock-only
    targets use ``upload_artifact_impl(..., allow_synthetic=True)``.
    """
    return await upload_artifact_impl(params)


async def upload_artifact_impl(
    params: UploadArtifactParams, *, allow_synthetic: bool = False
) -> ArtifactResult:
    """upload_artifact with the synthetic-artifact escape hatch.

    ``allow_synthetic`` is provenance-derived by INTERNAL callers only (a mock-only
    target group / a ``mock-`` device id) — it is deliberately not on the MCP tool
    signature or UploadArtifactParams, where a caller could simply assert it.
    """
    try:
        data = Path(params.file_path).read_bytes()
    except OSError as e:
        return ArtifactResult(error=f"Cannot read file: {e}")

    # Image-validity gate: a device downloads and BOOTS these bytes; anything that isn't
    # a plausible ESP32 image would sail through SHA-256 verification (computed over the
    # same bytes) and only fail on real hardware. Mocks never boot the artifact, so
    # synthetic uploads are allowed only when the caller proved a mock-only destination.
    if settings.esp_image_gate_enabled and not allow_synthetic:
        if len(data) < settings.esp_image_min_bytes or data[0] != _ESP_IMAGE_MAGIC:
            head = f"{data[0]:#04x}" if data else "empty"
            return ArtifactResult(
                error=(
                    f"artifact is not a valid ESP32 firmware image (first byte {head}, "
                    f"{len(data)} bytes; expected magic 0xE9 and >= "
                    f"{settings.esp_image_min_bytes} bytes). Synthetic blobs may only "
                    f"target mock-only device groups"
                )
            )

    checksum = sha256_bytes(data)
    signature: str | None = None

    if params.sign_key_path:
        try:
            key_pem = Path(params.sign_key_path).read_text()
            signature = sign_ed25519(key_pem, data)
        except Exception as e:
            return ArtifactResult(error=f"Signing failed: {e}")

    if settings.trusted_signing_key_path:
        if not signature:
            return ArtifactResult(error="Artifact must be signed: trusted_signing_key_path is configured")
        try:
            pub_pem = Path(settings.trusted_signing_key_path).read_text()
        except OSError as e:
            return ArtifactResult(error=f"Cannot read trusted key: {e}")
        if not verify_ed25519(pub_pem, data, signature):
            return ArtifactResult(error="Signature verification failed against trusted key")

    path = storage_path(params.version)

    try:
        await _upload_file(params.version, data)
    except Exception as e:
        return ArtifactResult(error=f"Storage upload failed: {e}")

    # Upsert by version: always (re)attach the deployable fields. Git-lineage columns
    # (build_id/branch_name/author/...) are written ONLY when the caller supplies them —
    # the nff-github connector does, a bare OTA upload does not — so a lineage-less
    # upload never nulls values a prior connector build wrote.
    row = {
        "version": params.version,
        "name": params.name,
        "device_types": params.device_types,
        "checksum_sha256": checksum,
        "size_bytes": len(data),
        "signature": signature,
        "storage_path": path,
        "provides": params.provides,
        "depends": params.depends,
    }

    client = get_client()

    for column, value in (
        ("project_id", params.project_id),
        ("build_id", params.build_id),
        ("branch_name", params.branch_name),
        ("author", params.author),
        ("committed_at", params.committed_at),
        ("changelog", params.changelog),
        ("release_version", params.release_version),
    ):
        if value is not None:
            row[column] = value
    if params.elf_path is not None:
        row["elf_path"] = params.elf_path
        row["elf_stored"] = True

    # parent_version is a FK to firmware_versions(version); only set it once that parent
    # row actually exists, or the upsert fails. A parent commit that was never built has
    # no row yet, so we drop the link rather than abort the upload.
    if params.parent_version:
        parent = (
            await client.table("firmware_versions")
            .select("version")
            .eq("version", params.parent_version)
            .execute()
        )
        if parent.data:
            row["parent_version"] = params.parent_version

    try:
        resp = await client.table("firmware_versions").upsert(row, on_conflict="version").execute()
        inserted = resp.data[0] if resp.data else row
    except Exception as e:
        return ArtifactResult(error=f"Database upsert failed: {e}")

    return ArtifactResult(artifact=_row_to_info(inserted))


async def list_artifacts(device_type: str | None = None, version_prefix: str | None = None) -> list[ArtifactInfo]:
    """List registered firmware artifacts (versions that carry a deployable binary)."""
    client = get_client()
    query = client.table("firmware_versions").select("*").order("version", desc=True)
    resp = await query.execute()
    rows = resp.data or []
    result = []
    for row in rows:
        if not row.get("storage_path"):
            continue  # a pure lineage row with no uploaded binary is not a deployable artifact
        dtypes = row.get("device_types") or []
        if isinstance(dtypes, str):
            dtypes = json.loads(dtypes)
        if device_type and device_type not in dtypes:
            continue
        if version_prefix and not row["version"].startswith(version_prefix):
            continue
        result.append(_row_to_info(row))
    return result


async def delete_artifact(version: str) -> ArtifactResult:
    """Unregister an artifact (remove its binary + deployable fields). Fails if any active deployment references the version.

    The firmware_versions row itself is preserved so version lineage / dashboard
    history and any FK references stay intact.
    """
    client = get_client()

    # Guard: check for active deployments
    active_statuses = ["scheduled", "active", "paused"]
    dep_resp = (
        await client.table("deployments")
        .select("id")
        .eq("target_version", version)
        .in_("status", active_statuses)
        .execute()
    )
    if dep_resp.data:
        return ArtifactResult(error=f"Cannot delete: {len(dep_resp.data)} active deployment(s) reference this version")

    # Fetch the version to confirm it exists and actually has a binary
    art_resp = await client.table("firmware_versions").select("*").eq("version", version).execute()
    if not art_resp.data:
        return ArtifactResult(error=f"Firmware version {version!r} not found")
    if not art_resp.data[0].get("storage_path"):
        return ArtifactResult(error=f"Firmware version {version!r} has no uploaded artifact to delete")

    try:
        await delete_artifact_file(version)
    except Exception as e:
        return ArtifactResult(error=f"Storage delete failed: {e}")

    # Drop the debug ELF too if one was stored. Best-effort: a missing ELF object must
    # not block unregistering the artifact.
    if art_resp.data[0].get("elf_stored"):
        try:
            await delete_elf(version)
        except Exception:
            pass

    cleared = {
        "checksum_sha256": None, "signature": None, "storage_path": None, "size_bytes": 0,
        "elf_stored": False, "elf_path": None,
    }
    await client.table("firmware_versions").update(cleared).eq("version", version).execute()
    return ArtifactResult(artifact=_row_to_info(art_resp.data[0]))


async def get_firmware_url(version: str, expires_in: int = 3600) -> ArtifactResult:
    """Generate a pre-signed HTTPS download URL for a stored firmware version.

    expires_in: seconds until the URL expires (default 3600 = 1 hour).
    This URL is embedded in the OTA command that start_deployment delivers through
    nff-fleet; the device downloads the BIN directly from it and verifies the sha256.
    """
    try:
        url = await _create_signed_url(version, expires_in)
        return ArtifactResult(download_url=url)
    except Exception as e:
        return ArtifactResult(error=f"Failed to generate download URL: {e}")


def _row_to_info(row: dict) -> ArtifactInfo:
    dtypes = row.get("device_types") or []
    if isinstance(dtypes, str):
        dtypes = json.loads(dtypes)
    provides = row.get("provides") or {}
    if isinstance(provides, str):
        provides = json.loads(provides)
    depends = row.get("depends") or {}
    if isinstance(depends, str):
        depends = json.loads(depends)
    return ArtifactInfo(
        id=row["version"],
        name=row.get("name") or row["version"],
        version=row["version"],
        device_types=dtypes,
        checksum_sha256=row.get("checksum_sha256") or "",
        size_bytes=row.get("size_bytes") or 0,
        signature=row.get("signature"),
        storage_path=row.get("storage_path") or "",
        provides=provides,
        depends=depends,
        created_at=str(row.get("created_at") or ""),
        build_id=row.get("build_id"),
        branch_name=row.get("branch_name"),
        author=row.get("author"),
        committed_at=str(row["committed_at"]) if row.get("committed_at") else None,
        changelog=row.get("changelog") or "",
        parent_version=row.get("parent_version"),
        elf_stored=bool(row.get("elf_stored")),
        elf_path=row.get("elf_path"),
        release_version=row.get("release_version"),
    )
