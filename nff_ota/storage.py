from nff_ota.client import get_client
from nff_ota.config import settings


def storage_path(artifact_id: str) -> str:
    """Deterministic bucket path: {first2}/{artifact_id}.bin"""
    return f"{artifact_id[:2]}/{artifact_id}.bin"


async def upload_artifact(artifact_id: str, data: bytes) -> str:
    path = storage_path(artifact_id)
    client = get_client()
    client.storage.from_(settings.artifact_bucket).upload(
        path=path,
        file=data,
        file_options={"content-type": "application/octet-stream"},
    )
    return path


async def download_artifact(artifact_id: str) -> bytes:
    path = storage_path(artifact_id)
    client = get_client()
    return client.storage.from_(settings.artifact_bucket).download(path)


async def delete_artifact_file(artifact_id: str) -> bool:
    path = storage_path(artifact_id)
    client = get_client()
    client.storage.from_(settings.artifact_bucket).remove([path])
    return True
