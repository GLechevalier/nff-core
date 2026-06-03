import asyncio
from typing import Any

from nff_fleet.broker.router import get_crash as _get_crash_cached
from nff_fleet.config import settings
from nff_fleet.ota import elf_path


def get_crash(device_id: str) -> dict[str, Any] | None:
    return _get_crash_cached(device_id)


async def resolve_backtrace(build_id: str, addrs: list[str]) -> str:
    elf = elf_path(build_id)
    if not elf.exists():
        return f"ELF not found for build_id={build_id} at {elf}"
    cmd = [
        "xtensa-esp32-elf-addr2line",
        "-pfiaC",
        "-e", str(elf),
        *addrs,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        return f"addr2line error (rc={proc.returncode}): {stderr.decode().strip()}"
    return stdout.decode().strip()
