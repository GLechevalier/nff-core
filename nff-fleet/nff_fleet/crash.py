import asyncio
from typing import Any

from nff_fleet.broker.router import get_crash as _get_crash_cached
from nff_fleet.config import settings
from nff_fleet.ota import elf_path

# ESP-IDF v5.3.x ships the unified toolchain `xtensa-esp-elf-*` (NOT the older
# `xtensa-esp32-elf-*`). An L2 (QEMU) firmware ELF is built with that toolchain, so its addr2line
# is the one that reads it. Both must match or resolution FileNotFoundErrors.
ADDR2LINE = "xtensa-esp-elf-addr2line"

# Xtensa return-address convention: a call pushes the address of the instruction AFTER the call,
# so a backtrace return address points one instruction past the call site. Subtract a few bytes so
# addr2line maps it to the call, not the next statement. Frame 0 is the faulting PC itself (exact),
# so it is NOT adjusted. Mirrors nff-diagnosis/nff_diagnosis/resolution/addr2line.py (_PC_ADJUST).
_PC_ADJUST = -3


def get_crash(device_id: str) -> dict[str, Any] | None:
    return _get_crash_cached(device_id)


def _adjust(addr: str, position: int) -> str:
    """Apply the Xtensa PC-3 return-address adjustment to frames beyond the fault PC (position>=1)."""
    if position == 0:
        return addr
    try:
        return hex(int(addr, 16) + _PC_ADJUST)
    except (ValueError, TypeError):
        return addr


def _parse_grouped(out: str, n: int) -> list[str]:
    """Parse `addr2line -pfiaC` output into one resolved string per input address (position-aligned).

    With `-a`, each frame group starts with a `0xADDR: func at file:line` line; inlined frames
    follow on their own lines. We strip the address prefix and join a group's lines with ' | '.
    Pad/truncate to `n` so the result aligns 1:1 with the input addresses.
    """
    groups: list[str] = []
    cur: list[str] | None = None
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("0x"):
            if cur is not None:
                groups.append(" | ".join(cur))
            _, sep, rest = stripped.partition(": ")
            cur = [(rest.strip() if sep else stripped)]
        elif cur is not None and stripped:
            cur.append(stripped)
    if cur is not None:
        groups.append(" | ".join(cur))
    if len(groups) < n:
        groups += [""] * (n - len(groups))
    return groups[:n]


async def resolve_frames(build_id: str, addrs: list[str]) -> list[str] | None:
    """Resolve raw backtrace addresses to `func at file:line`, one string per input address.

    Best-effort: returns None (never raises) when the build_id/ELF is missing or the addr2line
    binary is not installed, so a caller (e.g. crash ingest) can fall back to storing raw
    addresses. Applies the Xtensa PC-3 adjustment to return-address frames.
    """
    if not build_id or not addrs:
        return None
    elf = elf_path(build_id)
    if not elf.exists():
        return None
    adjusted = [_adjust(a, i) for i, a in enumerate(addrs)]
    try:
        proc = await asyncio.create_subprocess_exec(
            ADDR2LINE, "-pfiaC", "-e", str(elf), *adjusted,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError):
        return None  # toolchain not present in this image
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    return _parse_grouped(stdout.decode(errors="replace"), len(addrs))


async def resolve_backtrace(build_id: str, addrs: list[str]) -> str:
    """Resolve a backtrace to a single human-readable text blob (the fleet_resolve_backtrace tool)."""
    elf = elf_path(build_id)
    if not elf.exists():
        return f"ELF not found for build_id={build_id} at {elf}"
    cmd = [
        ADDR2LINE,
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
