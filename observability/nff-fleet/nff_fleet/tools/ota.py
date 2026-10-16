"""ELF staging tool for crash symbolication.

OTA delivery is no longer handled by nff-fleet — nff-ota is the OTA brain and asks
fleet to deliver via the internal /internal/command endpoint (nff_fleet/control.py).
The only tool that remains here stages the ELF used by fleet_resolve_backtrace
(addr2line); this is a crash/diagnosis concern slated to move to nff-diagnosis.
"""

import base64
from typing import Any

from nff_fleet import ota as ota_module


async def fleet_store_elf(build_id: str, elf_bytes_b64: str) -> dict[str, Any]:
    """Store a base64-encoded ELF binary under /data/firmware/{build_id}/firmware.elf.

    Used by fleet_resolve_backtrace to symbolicate crash backtraces. This is a
    crash-diagnosis helper, not OTA.
    """
    data = base64.b64decode(elf_bytes_b64)
    dest = await ota_module.store_elf(build_id, data)
    return {"build_id": build_id, "path": str(dest), "size_bytes": len(data)}


async def fleet_store_bin(build_id: str, bin_bytes_b64: str) -> dict[str, Any]:
    """Store a base64-encoded merged flash image under /data/firmware/{build_id}/firmware.bin.

    Used by the L2 supervisor: it reads this image off the shared /data mount to seed a fresh
    emulated device's flash. Mirrors fleet_store_bin's ELF sibling.
    """
    data = base64.b64decode(bin_bytes_b64)
    dest = await ota_module.store_bin(build_id, data)
    return {"build_id": build_id, "path": str(dest), "size_bytes": len(data)}
