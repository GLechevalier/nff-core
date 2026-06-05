"""ELF staging for crash symbolication.

NOTE: OTA delivery has moved entirely to nff-ota. nff-fleet only *sends commands*
(see nff_fleet/control.py). What remains here is ELF storage used by the crash
path (nff_fleet/crash.py:resolve_backtrace runs addr2line against the stored ELF).
This belongs to the crash/diagnosis concern and is slated to move to nff-diagnosis
in a separate effort; it is kept here only because the crash path still uses it.
"""

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
