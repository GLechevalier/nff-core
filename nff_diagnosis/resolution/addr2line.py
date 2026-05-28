import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from nff_diagnosis.schemas import CrashRecord
from nff_diagnosis.schemas.diagnosis_output import ResolvedFrame

_XTENSA_ADDR2LINE = "xtensa-esp32-elf-addr2line"
_PC_ADJUST = -3  # Xtensa: caller PC is 3 bytes ahead of actual call site
_FRAME_RE = re.compile(r"^(.+?) at (.+?):(\d+)$")


def _adjust_pc(addr: str) -> str:
    """Subtract 3 from a hex address string, keeping 0x prefix."""
    try:
        adjusted = int(addr, 16) + _PC_ADJUST
        return hex(adjusted)
    except ValueError:
        return addr


def _parse_frame(addr: str, line: str) -> ResolvedFrame:
    m = _FRAME_RE.match(line.strip())
    if not m:
        return ResolvedFrame(pc=addr)
    fn, file_, lineno = m.group(1), m.group(2), int(m.group(3))
    return ResolvedFrame(
        pc=addr,
        function=fn if fn != "??" else None,
        file=file_ if (file_ and "??" not in file_) else None,
        line=lineno if (lineno != 0 and file_ and "??" not in file_) else None,
    )


class Addr2LineResolver:
    def __init__(self, addr2line_exe: str = _XTENSA_ADDR2LINE) -> None:
        self._exe = addr2line_exe

    def resolve(
        self,
        crash: CrashRecord,
        elf_path: Path | None,
    ) -> tuple[list[ResolvedFrame], dict[str, Any]]:
        """
        Returns (resolved_backtrace, location).
        Degrades gracefully when ELF or addr2line is unavailable.
        """
        addresses = list(crash.backtrace)
        if crash.fault_pc and crash.fault_pc not in addresses:
            addresses = [crash.fault_pc] + addresses

        if not addresses:
            return [], {}

        frames = self._resolve_addresses(addresses, elf_path)

        bt_pcs = set(crash.backtrace)
        resolved_bt = [f for f in frames if f.pc in bt_pcs]
        # Preserve original backtrace order
        pc_to_frame: dict[str, ResolvedFrame] = {f.pc: f for f in frames}
        resolved_bt = [pc_to_frame.get(pc, ResolvedFrame(pc=pc)) for pc in crash.backtrace]

        # location = first frame with meaningful symbols
        location: dict[str, Any] = {}
        for frame in resolved_bt:
            if frame.function and frame.file:
                location = {
                    "function": frame.function,
                    "file": frame.file,
                    "line": frame.line,
                }
                break

        return resolved_bt, location

    def _resolve_addresses(
        self,
        addresses: list[str],
        elf_path: Path | None,
    ) -> list[ResolvedFrame]:
        if elf_path is None:
            print("WARNING: no ELF path; backtrace unresolved", file=sys.stderr)
            return [ResolvedFrame(pc=addr) for addr in addresses]

        adjusted = [_adjust_pc(addr) for addr in addresses]
        try:
            result = subprocess.run(
                [self._exe, "-fipe", str(elf_path), *adjusted],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            print(f"WARNING: {self._exe!r} not found; backtrace unresolved", file=sys.stderr)
            return [ResolvedFrame(pc=addr) for addr in addresses]

        lines = result.stdout.splitlines()
        frames: list[ResolvedFrame] = []
        for addr, line in zip(addresses, lines):
            frames.append(_parse_frame(addr, line))
        # If addr2line returned fewer lines than addresses, pad with unresolved
        for addr in addresses[len(lines):]:
            frames.append(ResolvedFrame(pc=addr))
        return frames
