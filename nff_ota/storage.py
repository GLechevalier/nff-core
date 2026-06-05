import re

from nff_ota.client import get_client
from nff_ota.config import settings


def storage_path(version: str) -> str:
    """Deterministic bucket path for a firmware version: firmware/{safe_version}.bin

    The version is the firmware_versions PK; sanitise it so it is a safe object key.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", version)
    return f"firmware/{safe}.bin"


async def upload_artifact(version: str, data: bytes) -> str:
    path = storage_path(version)
    client = get_client()
    await client.storage.from_(settings.artifact_bucket).upload(
        path=path,
        file=data,
        file_options={"content-type": "application/octet-stream", "upsert": "true"},
    )
    return path


async def download_artifact(version: str) -> bytes:
    path = storage_path(version)
    client = get_client()
    return await client.storage.from_(settings.artifact_bucket).download(path)


async def create_signed_url(version: str, expires_in: int = 3600) -> str:
    """Return a time-limited pre-signed HTTPS download URL for the firmware binary."""
    path = storage_path(version)
    client = get_client()
    res = await client.storage.from_(settings.artifact_bucket).create_signed_url(path, expires_in)
    return res["signedURL"]


async def delete_artifact_file(version: str) -> bool:
    path = storage_path(version)
    client = get_client()
    await client.storage.from_(settings.artifact_bucket).remove([path])
    return True
