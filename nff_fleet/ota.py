import base64
import hashlib
from pathlib import Path

import aiofiles

from nff_fleet.config import settings


def elf_path(build_id: str) -> Path:
    return settings.firmware_dir / build_id / "firmware.elf"


async def store_elf(build_id: str, data: bytes) -> Path:
    dest = elf_path(build_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(dest, "wb") as f:
        await f.write(data)
    return dest


def ota_payload(build_id: str, firmware_url: str) -> dict:
    sha256 = hashlib.sha256(elf_path(build_id).read_bytes()).hexdigest()
    return {
        "action": "ota",
        "version": build_id,
        "sha256": sha256,
        "url": firmware_url,
    }
