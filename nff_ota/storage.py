import re

from nff_ota.client import get_client
from nff_ota.config import settings


def _safe_key(version: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", version)


def storage_path(version: str) -> str:
    """Deterministic bucket path for a firmware version: firmware/{safe_version}.bin

    The version is the firmware_versions PK; sanitise it so it is a safe object key.
    """
    return f"firmware/{_safe_key(version)}.bin"


def elf_storage_path(version: str) -> str:
    """Bucket path for a version's debug ELF: firmware/{safe_version}.elf

    Sibling of the deployable ``.bin``. The ELF is kept for crash symbolication
    (addr2line over a device-reported backtrace); it is never delivered to a device.
    """
    return f"firmware/{_safe_key(version)}.elf"


async def upload_elf(version: str, data: bytes) -> str:
    """Store the debug ELF for a version and return its bucket path."""
    path = elf_storage_path(version)
    client = get_client()
    await client.storage.from_(settings.artifact_bucket).upload(
        path=path,
        file=data,
        file_options={"content-type": "application/octet-stream", "upsert": "true"},
    )
    return path


async def delete_elf(version: str) -> bool:
    path = elf_storage_path(version)
    client = get_client()
    await client.storage.from_(settings.artifact_bucket).remove([path])
    return True


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
