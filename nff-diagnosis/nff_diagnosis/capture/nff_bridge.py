import subprocess
from pathlib import Path


class NffBridge:
    """Thin wrapper that delegates all device I/O to the `nff` CLI binary."""

    def __init__(self, nff_exe: str = "nff") -> None:
        self._exe = nff_exe

    def flash(self, sketch_path: Path, port: str, board: str) -> str:
        result = subprocess.run(
            [self._exe, "flash", str(sketch_path), "--port", port, "--board", board],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"nff flash failed (exit {result.returncode}): {result.stderr.strip()}")
        return result.stdout

    def serial_read(self, port: str, duration_ms: int, baud: int = 115200) -> str:
        result = subprocess.run(
            [
                self._exe,
                "serial-read",
                "--port", port,
                "--duration", str(duration_ms),
                "--baud", str(baud),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"nff serial-read failed (exit {result.returncode}): {result.stderr.strip()}")
        return result.stdout
