from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from nff_ota.client import get_client
from nff_ota.config import settings
from nff_ota.crypto import sha256_bytes, sign_ed25519, verify_ed25519
from nff_ota.models.artifact import ArtifactInfo, ArtifactResult, UploadArtifactParams
from nff_ota.storage import delete_artifact_file, storage_path, upload_artifact as _upload_file


async def upload_artifact(params: UploadArtifactParams) -> ArtifactResult:
    """Upload a firmware artifact and register it. The file is read from file_path on the server."""
    try:
        data = Path(params.file_path).read_bytes()
    except OSError as e:
        return ArtifactResult(error=f"Cannot read file: {e}")

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

    artifact_id = str(uuid.uuid4())
    path = storage_path(artifact_id)

    try:
        await _upload_file(artifact_id, data)
    except Exception as e:
        return ArtifactResult(error=f"Storage upload failed: {e}")

    row = {
        "id": artifact_id,
        "name": params.name,
        "version": params.version,
        "device_types": params.device_types,
        "checksum_sha256": checksum,
        "size_bytes": len(data),
        "signature": signature,
        "storage_path": path,
        "provides": params.provides,
        "depends": params.depends,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        client = get_client()
        resp = client.table("artifacts").insert(row).execute()
        if resp.error:
            return ArtifactResult(error=str(resp.error))
        inserted = resp.data[0] if resp.data else row
    except Exception as e:
        return ArtifactResult(error=f"Database insert failed: {e}")

    return ArtifactResult(artifact=_row_to_info(inserted))


async def list_artifacts(device_type: str | None = None, version_prefix: str | None = None) -> list[ArtifactInfo]:
    """List all registered artifacts, optionally filtered by device_type or version prefix."""
    client = get_client()
    query = client.table("artifacts").select("*").order("created_at", desc=True)
    resp = query.execute()
    rows = resp.data or []
    result = []
    for row in rows:
        dtypes = row.get("device_types") or []
        if isinstance(dtypes, str):
            dtypes = json.loads(dtypes)
        if device_type and device_type not in dtypes:
            continue
        if version_prefix and not row["version"].startswith(version_prefix):
            continue
        result.append(_row_to_info(row))
    return result


async def delete_artifact(artifact_id: str) -> ArtifactResult:
    """Delete an artifact. Fails if any active (non-terminal) deployment references it."""
    client = get_client()

    # Guard: check for active deployments
    active_statuses = ["scheduled", "active", "paused"]
    dep_resp = (
        client.table("deployments")
        .select("id")
        .eq("artifact_id", artifact_id)
        .in_("status", active_statuses)
        .execute()
    )
    if dep_resp.data:
        return ArtifactResult(error=f"Cannot delete: {len(dep_resp.data)} active deployment(s) reference this artifact")

    # Fetch artifact to confirm it exists
    art_resp = client.table("artifacts").select("*").eq("id", artifact_id).execute()
    if not art_resp.data:
        return ArtifactResult(error=f"Artifact {artifact_id!r} not found")

    try:
        await delete_artifact_file(artifact_id)
    except Exception as e:
        return ArtifactResult(error=f"Storage delete failed: {e}")

    client.table("artifacts").delete().eq("id", artifact_id).execute()
    return ArtifactResult(artifact=_row_to_info(art_resp.data[0]))


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
        id=row["id"],
        name=row["name"],
        version=row["version"],
        device_types=dtypes,
        checksum_sha256=row["checksum_sha256"],
        size_bytes=row["size_bytes"],
        signature=row.get("signature"),
        storage_path=row["storage_path"],
        provides=provides,
        depends=depends,
        created_at=str(row["created_at"]),
    )
