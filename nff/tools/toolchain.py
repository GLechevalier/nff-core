"""Firmware build dispatcher + arduino-cli backend.

This module hosts the arduino-cli implementation (compile/upload/stream, sketch
management, flashing) AND the thin dispatch layer that routes the public entry points
(``compile_only``, ``flash``, ``stream_compile``, ``stream_upload``,
``resolve_sketch_dir``, ``discover_artifacts``) to the active build backend. The
default backend is arduino-cli (these functions, unchanged); set
``NFF_BUILD_BACKEND=platformio`` (or ``build.backend`` in config) to route to
:mod:`nff.tools.backends.platformio` instead. Callers keep using ``toolchain.*``.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional, Tuple

from nff import config
from nff.tools import retry as _retry

_SKETCH_DIR = Path(tempfile.gettempdir()) / "nff_sketch"


def active_backend() -> str:
    """The selected build backend: "platformio" (the default) or "arduino".

    Precedence: ``NFF_BUILD_BACKEND`` env var → ``build.backend`` in config →
    "platformio". "pio" is accepted as an alias for "platformio"; only an explicit
    "arduino"/"arduino-cli" selects the arduino-cli backend.
    """
    name = os.environ.get("NFF_BUILD_BACKEND")
    if not name:
        try:
            name = (config.get_build_config() or {}).get("backend")
        except Exception:
            name = None
    name = (name or "platformio").strip().lower()
    return "arduino" if name in ("arduino", "arduino-cli") else "platformio"


def _pio_active() -> bool:
    return active_backend() == "platformio"


def package_recover(board: str) -> Optional[Callable[[str], None]]:
    """A between-retries repair hook for the active backend's package faults, or None.

    On the PlatformIO backend this prunes a half-installed platform so a retried build
    reinstalls clean; the arduino backend has nothing to repair this way.
    """
    if _pio_active():
        from nff.tools.backends import platformio as _pio
        return lambda out: _pio._recover_packages(out, board)
    return None


def configured_board() -> str:
    """The board identifier for the active backend, or "" if unset.

    For platformio this is ``build.board`` (a PlatformIO board id, e.g. ``esp32dev``),
    falling back to the legacy ``default_device.fqbn``; for arduino it is
    ``default_device.fqbn`` (an arduino-cli FQBN).
    """
    try:
        fqbn = config.get_default_device().get("fqbn") or ""
        if _pio_active():
            # 1. an explicit build.board (set by `nff init`);
            board = (config.get_build_config() or {}).get("board")
            if board:
                return board
            # 2. derive a PlatformIO board id from the detected arduino FQBN, so the pio
            #    backend works without --board even when build.board was never persisted;
            if fqbn:
                from nff.tools import boards
                pio_board = boards.fqbn_to_pio_board(fqbn)
                if pio_board:
                    return pio_board
            # 3. last resort: hand the fqbn through.
        return fqbn
    except Exception:
        return ""

# Command timeouts (seconds). The generic _RUN_TIMEOUT covers short commands;
# a cold ESP32 first build easily exceeds 120s, so compile gets its own ceiling.
_RUN_TIMEOUT = 120
_COMPILE_TIMEOUT = 600
_UPLOAD_TIMEOUT = 180


class ToolchainError(Exception):
    pass


@dataclass
class RunResult:
    success: bool
    stdout: str
    stderr: str
    returncode: int

    @property
    def output(self) -> str:
        parts = [s.strip() for s in (self.stdout, self.stderr) if s.strip()]
        return "\n".join(parts)


@dataclass
class CompileResult:
    """Structured outcome of a compile-only run — no upload, no port required.

    ``ok`` is the single field a caller should branch on. ``artifacts`` maps a
    short kind ("elf", "bin", "merged_bin", "hex", ...) to its absolute path so
    the caller never has to guess whether it got an ELF or a binary. ``image``
    is the one file you would flash to hardware (merged.bin > bin > hex).
    """

    ok: bool
    fqbn: str
    sketch_dir: Path
    returncode: int
    output: str
    artifacts: dict[str, Path] = field(default_factory=dict)

    @property
    def errors(self) -> list[str]:
        return [ln for ln in self.output.splitlines() if "error:" in ln.lower()]

    @property
    def elf(self) -> Optional[Path]:
        return self.artifacts.get("elf")

    @property
    def image(self) -> Optional[Path]:
        for kind in ("merged_bin", "bin", "hex"):
            if kind in self.artifacts:
                return self.artifacts[kind]
        return None

    def summary(self) -> str:
        """Human/agent-readable one-screen summary — unambiguous about what was produced."""
        if not self.ok:
            errs = self.errors or [self.output.strip() or "unknown compile error"]
            head = "\n".join(errs[:20])
            return f"ERROR: compile failed for {self.fqbn}\n{head}"
        lines = [f"OK: compile succeeded for {self.fqbn}"]
        size = _extract_size(self.output)
        if size:
            lines.append(size)
        if self.elf:
            lines.append(f"elf:   {self.elf}")
        if self.image and self.image != self.elf:
            lines.append(f"image: {self.image}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "fqbn": self.fqbn,
            "sketch_dir": str(self.sketch_dir),
            "returncode": self.returncode,
            "elf": str(self.elf) if self.elf else None,
            "image": str(self.image) if self.image else None,
            "artifacts": {k: str(v) for k, v in self.artifacts.items()},
            "errors": self.errors,
            "output": self.output,
        }


_SIZE_RE = re.compile(r"Sketch uses .*?\)", re.IGNORECASE)


def _extract_size(output: str) -> Optional[str]:
    m = _SIZE_RE.search(output)
    return m.group(0) if m else None


class ProcessStream:
    def __init__(self, cmd: list[str]) -> None:
        self._cmd = cmd
        self.returncode: Optional[int] = None

    def __iter__(self) -> Iterator[str]:
        try:
            proc = subprocess.Popen(
                self._cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except FileNotFoundError:
            raise ToolchainError(f"Executable not found: {self._cmd[0]}")
        assert proc.stdout is not None
        for line in proc.stdout:
            yield line.rstrip("\n")
        proc.wait()
        self.returncode = proc.returncode


def _arduino_cli_fallback_path() -> Optional[str]:
    if sys.platform == "win32":
        import os
        base = os.environ.get("LOCALAPPDATA", "")
        candidate = Path(base) / "Programs" / "arduino-cli" / "arduino-cli.exe"
    else:
        candidate = Path.home() / ".local" / "bin" / "arduino-cli"
    return str(candidate) if candidate.exists() else None


def find_arduino_cli() -> Optional[str]:
    found = shutil.which("arduino-cli")
    if found:
        return found
    return _arduino_cli_fallback_path()


def find_esptool() -> Optional[str]:
    return shutil.which("esptool.py") or shutil.which("esptool")


def _version_of(cmd: list[str]) -> Optional[str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return (r.stdout or r.stderr).strip().splitlines()[0]
    except Exception:
        pass
    return None


def arduino_cli_version() -> Optional[str]:
    cli = find_arduino_cli()
    if not cli:
        return None
    return _version_of([cli, "version"])


def esptool_version() -> Optional[str]:
    tool = find_esptool()
    if not tool:
        return None
    return _version_of([tool, "version"])


def _require_arduino_cli() -> str:
    cli = find_arduino_cli()
    if not cli:
        raise ToolchainError("arduino-cli not found — run `nff install-deps`")
    return cli


def ensure_build_tool() -> None:
    """Raise a ToolchainError with actionable `nff install-deps` guidance if the active
    backend's build tool is missing — so the streaming flash path fails with the same
    hint `compile_only`/`doctor` give, not a bare 'Executable not found: pio'."""
    if _pio_active():
        from nff.tools.backends import platformio as _pio
        if _pio.find_platformio() is None:
            raise ToolchainError("platformio not found — run `nff install-deps`")
    elif find_arduino_cli() is None:
        raise ToolchainError("arduino-cli not found — run `nff install-deps`")


def _run(cmd: list[str], timeout: int = _RUN_TIMEOUT) -> RunResult:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return RunResult(
            success=r.returncode == 0,
            stdout=r.stdout or "",
            stderr=r.stderr or "",
            returncode=r.returncode,
        )
    except FileNotFoundError:
        # A missing binary is never transient — fail hard, never retry.
        raise ToolchainError(f"Executable not found: {cmd[0]}")
    except subprocess.TimeoutExpired as exc:
        # Surface a timeout as a failed RunResult (not a raise) so the retry
        # layer can classify it as transient and try again. rc=124 mirrors the
        # shell timeout convention.
        partial = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return RunResult(
            success=False,
            stdout=partial,
            stderr=f"Command timed out after {timeout}s: {cmd[0]}",
            returncode=124,
        )


def run_arduino_cli(args: list[str], timeout: int = 300) -> RunResult:
    """Run an arbitrary arduino-cli subcommand (e.g. ["core", "install", ...]).

    Raises ToolchainError if arduino-cli is missing or the command times out.
    """
    cli = _require_arduino_cli()
    return _run([cli, *args], timeout=timeout)


def stream_arduino_cli(args: list[str]) -> ProcessStream:
    """Stream an arbitrary arduino-cli subcommand line-by-line (for long installs).

    Raises ToolchainError if arduino-cli is missing.
    """
    cli = _require_arduino_cli()
    return ProcessStream([cli, *args])


def write_sketch(code: str, sketch_dir: Optional[Path] = None) -> Path:
    if sketch_dir is None:
        sketch_dir = _SKETCH_DIR
    sketch_dir = Path(sketch_dir)
    sketch_dir.mkdir(parents=True, exist_ok=True)
    ino = sketch_dir / f"{sketch_dir.name}.ino"
    ino.write_text(code, encoding="utf-8")
    return sketch_dir


def resolve_sketch_dir(
    code: Optional[str] = None,
    source: Optional[Path] = None,
    sketch_dir: Optional[Path] = None,
) -> Path:
    """Return a ready-to-compile sketch directory from any reasonable input.

    arduino-cli requires the sketch live in a folder whose name matches the
    ``.ino`` file. This normalises the three ways a caller can point at a
    sketch so they all "just work":

    - ``source`` is a directory  → used as-is (must contain ``<name>.ino``).
    - ``source`` is a ``.ino``    → its parent folder, if the parent name
      matches the file stem; otherwise the sketch is copied into a temp folder
      named after the stem (so a loose ``blink.ino`` still compiles).
    - ``code`` is a string        → written to ``sketch_dir`` (default temp).

    Under the platformio backend this delegates to the pio project scaffold instead
    (``src/main.cpp`` + generated ``platformio.ini``), so the returned path is a
    ready-to-build PlatformIO project rather than an arduino sketch folder.
    """
    if _pio_active():
        from nff.tools.backends import platformio as _pio
        return _pio.resolve_project(code=code, source=source, sketch_dir=sketch_dir)
    if source is not None:
        src = Path(source)
        if src.is_dir():
            inos = sorted(src.glob("*.ino"))
            if not inos:
                raise ToolchainError(f"No .ino file found in {src}")
            return src
        if src.suffix == ".ino" and src.is_file():
            parent = src.parent
            if parent.name == src.stem:
                return parent
            dest = (sketch_dir or _SKETCH_DIR / src.stem)
            dest = Path(dest)
            dest.mkdir(parents=True, exist_ok=True)
            (dest / f"{dest.name}.ino").write_text(
                src.read_text(encoding="utf-8"), encoding="utf-8"
            )
            return dest
        raise ToolchainError(f"Not a sketch file or directory: {src}")
    if code is not None:
        return write_sketch(code, sketch_dir)
    raise ToolchainError("provide either code= or source= (a .ino file or sketch folder)")


def _build_dir(sketch_dir: Path, fqbn: str) -> Path:
    return sketch_dir / "build" / fqbn.replace(":", ".")


def elf_path_for(sketch_dir: Path, fqbn: str) -> Path:
    return _build_dir(sketch_dir, fqbn) / f"{sketch_dir.name}.ino.elf"


# kind → filename suffix, in flash-priority order for the "image" pick
_ARTIFACT_SUFFIXES = {
    "elf": ".ino.elf",
    "merged_bin": ".ino.merged.bin",
    "bin": ".ino.bin",
    "hex": ".ino.hex",
    "partitions_bin": ".ino.partitions.bin",
    "bootloader_bin": ".ino.bootloader.bin",
}


def discover_artifacts(sketch_dir: Path, fqbn: str) -> dict[str, Path]:
    """Map artifact kind → absolute path for whatever the compile produced.

    Looks first at the deterministic build dir, then falls back to a recursive
    scan so a stray build layout still resolves instead of crashing.
    """
    if _pio_active():
        from nff.tools.backends import platformio as _pio
        return _pio.discover_artifacts(sketch_dir, fqbn)
    build_dir = _build_dir(sketch_dir, fqbn)
    name = sketch_dir.name
    found: dict[str, Path] = {}
    for kind, suffix in _ARTIFACT_SUFFIXES.items():
        candidate = build_dir / f"{name}{suffix}"
        if candidate.exists():
            found[kind] = candidate
    if "elf" not in found:
        for p in sketch_dir.rglob("*.elf"):
            if p.is_file():
                found["elf"] = p
                break
    if "merged_bin" not in found and "bin" not in found and "hex" not in found:
        for ext, kind in ((".merged.bin", "merged_bin"), (".bin", "bin"), (".hex", "hex")):
            for p in sketch_dir.rglob(f"*{ext}"):
                if p.is_file():
                    found[kind] = p
                    break
            if kind in found:
                break
    return found


def locate_compiled_elf(sketch_dir: Path, fqbn: str) -> Path:
    arts = discover_artifacts(sketch_dir, fqbn)
    elf = arts.get("elf")
    if elf is not None:
        return elf
    raise ToolchainError(f"Could not find compiled ELF in {sketch_dir}")


def _fqbn_build_property(fqbn: str) -> list[str]:
    """arduino-cli args that bake the fqbn into the firmware as NFF_FQBN_TOKEN.

    The nff SDK stringifies NFF_FQBN_TOKEN into its heartbeat so a device reports
    exactly what it was built as. Passed as a bare (unquoted) token to dodge
    cross-shell quoting of the colons in an fqbn. ``compiler.cpp.extra_flags`` is
    the conventional empty user-flag slot, so overriding it clobbers nothing.
    """
    return ["--build-property", f"compiler.cpp.extra_flags=-DNFF_FQBN_TOKEN={fqbn}"]


def compile_sketch(sketch_dir: Path, fqbn: str) -> RunResult:
    cli = _require_arduino_cli()
    build_path = _build_dir(sketch_dir, fqbn)
    build_path.mkdir(parents=True, exist_ok=True)
    cmd = [cli, "compile", "--fqbn", fqbn, *_fqbn_build_property(fqbn),
           "--build-path", str(build_path), str(sketch_dir)]
    # Retry transient toolchain hiccups; a genuine compile error fails fast.
    return _retry.run_with_retry(lambda: _run(cmd, timeout=_COMPILE_TIMEOUT))


def upload_sketch(sketch_dir: Path, fqbn: str, port: str) -> RunResult:
    cli = _require_arduino_cli()
    build_path = _build_dir(sketch_dir, fqbn)
    # Reuse the artifacts already built by compile_sketch instead of rebuilding.
    cmd = [cli, "upload", "--fqbn", fqbn, "--port", port]
    if build_path.exists():
        cmd += ["--input-dir", str(build_path)]
    cmd.append(str(sketch_dir))
    # Longer backoff: the board re-enumerates after arduino-cli's auto-reset.
    return _retry.run_with_retry(
        lambda: _run(cmd, timeout=_UPLOAD_TIMEOUT), backoff=(2.0, 4.0)
    )


def stream_compile(sketch_dir: Path, fqbn: str) -> ProcessStream:
    if _pio_active():
        from nff.tools.backends import platformio as _pio
        return _pio.stream_compile(sketch_dir, fqbn)
    cli = _require_arduino_cli()
    build_path = _build_dir(sketch_dir, fqbn)
    build_path.mkdir(parents=True, exist_ok=True)
    return ProcessStream([cli, "compile", "--fqbn", fqbn, *_fqbn_build_property(fqbn),
                          "--build-path", str(build_path), str(sketch_dir)])


def stream_upload(sketch_dir: Path, fqbn: str, port: str) -> ProcessStream:
    if _pio_active():
        from nff.tools.backends import platformio as _pio
        return _pio.stream_upload(sketch_dir, fqbn, port)
    cli = _require_arduino_cli()
    build_path = _build_dir(sketch_dir, fqbn)
    cmd = [cli, "upload", "--fqbn", fqbn, "--port", port]
    if build_path.exists():
        cmd += ["--input-dir", str(build_path)]
    cmd.append(str(sketch_dir))
    return ProcessStream(cmd)


def run_stream_attempt(
    stream: ProcessStream, emit: Callable[[str], None]
) -> Tuple[int, str]:
    """Consume one ProcessStream, echoing each line live, and return
    ``(returncode, captured_output)`` so a retry layer can classify the result."""
    captured: list[str] = []
    for line in stream:
        emit(line)
        captured.append(line)
    return (stream.returncode or 0), "\n".join(captured)


def stream_with_retry(
    make_stream: Callable[[], ProcessStream],
    emit: Callable[[str], None],
    *,
    backoff: Tuple[float, ...] = _retry.DEFAULT_BACKOFF,
    recover: Optional[Callable[[str], None]] = None,
) -> int:
    """Run a live-streaming arduino-cli command with transient-failure retry.

    ``make_stream`` builds a fresh ProcessStream per attempt (a stream is
    single-use). Output is echoed live via ``emit``; a transient non-zero result
    is retried after a banner. ``recover``, if given, is called with the captured
    output between attempts for best-effort repair. Returns the final returncode.
    """
    return _retry.stream_with_retry(
        make_stream, emit, run_stream_attempt, backoff=backoff, recover=recover
    )


def compile_only(
    fqbn: str,
    code: Optional[str] = None,
    source: Optional[Path] = None,
    sketch_dir: Optional[Path] = None,
) -> CompileResult:
    """Compile a sketch and report exactly what came out — never uploads.

    Accepts a ``.ino`` file, a sketch folder (``source=``) or raw ``code=``.
    Always returns a :class:`CompileResult`; only ``find_arduino_cli`` /
    sketch-resolution problems raise, so callers can branch on ``.ok``.
    """
    if not fqbn:
        raise ToolchainError(
            "Missing board (FQBN or PlatformIO board id) — pass board=/--board "
            "or run `nff init`"
        )
    if _pio_active():
        from nff.tools.backends import platformio as _pio
        if not _pio.find_platformio():
            raise ToolchainError("platformio not found — run `nff install-deps`")
        sd = _pio.resolve_project(code=code, source=source, sketch_dir=sketch_dir)
        result = _pio.compile_sketch(sd, fqbn)
        artifacts = _pio.discover_artifacts(sd, fqbn) if result.success else {}
        return CompileResult(
            ok=result.success,
            fqbn=fqbn,
            sketch_dir=sd,
            returncode=result.returncode,
            output=result.output,
            artifacts=artifacts,
        )
    if not find_arduino_cli():
        raise ToolchainError("arduino-cli not found — run `nff install-deps`")
    sd = resolve_sketch_dir(code=code, source=source, sketch_dir=sketch_dir)
    result = compile_sketch(sd, fqbn)
    artifacts = discover_artifacts(sd, fqbn) if result.success else {}
    return CompileResult(
        ok=result.success,
        fqbn=fqbn,
        sketch_dir=sd,
        returncode=result.returncode,
        output=result.output,
        artifacts=artifacts,
    )


def compile(code: str, fqbn: str) -> tuple[str, Path]:
    """Compile from raw code, returning (output, elf_path)."""
    result = compile_only(fqbn, code=code)
    elf = result.elf if result.ok else Path("")
    return result.output, elf if elf is not None else Path("")


def flash(
    code: Optional[str] = None,
    fqbn: str = "",
    port: str = "",
    sketch_dir: Optional[Path] = None,
    source: Optional[Path] = None,
) -> str:
    """Compile then upload. Compile failures are reported distinctly from
    upload failures so the caller knows which half went wrong."""
    if _pio_active():
        from nff.tools.backends import platformio as _pio
        if not _pio.find_platformio():
            return "ERROR: platformio not found — run `nff install-deps`"
        try:
            sd = _pio.resolve_project(code=code, source=source, sketch_dir=sketch_dir)
        except (OSError, ToolchainError) as exc:
            return f"ERROR: {exc}"
        compile_result = _pio.compile_sketch(sd, fqbn)
        if not compile_result.success:
            return f"ERROR: Compile failed:\n{compile_result.output}"
        upload_result = _pio.upload_sketch(sd, fqbn, port)
        if not upload_result.success:
            return f"ERROR: Upload failed:\n{upload_result.output}"
        return (f"OK: flash complete\n--- compile ---\n{compile_result.output}\n"
                f"--- upload ---\n{upload_result.output}")
    try:
        sd = resolve_sketch_dir(code=code, source=source, sketch_dir=sketch_dir)
    except (OSError, ToolchainError) as exc:
        return f"ERROR: {exc}"
    if not find_arduino_cli():
        return "ERROR: arduino-cli not found — run `nff install-deps`"
    compile_result = compile_sketch(sd, fqbn)
    if not compile_result.success:
        return f"ERROR: Compile failed:\n{compile_result.output}"
    upload_result = upload_sketch(sd, fqbn, port)
    if not upload_result.success:
        return f"ERROR: Upload failed:\n{upload_result.output}"
    return f"OK: flash complete\n--- compile ---\n{compile_result.output}\n--- upload ---\n{upload_result.output}"


def esptool_flash(port: str, bin_path: Path, baud: int = 921600, address: str = "0x0") -> str:
    tool = find_esptool()
    if not tool:
        return "ERROR: esptool not found — run `nff install-deps`"
    result = _run([tool, "--port", port, "--baud", str(baud),
                   "write_flash", address, str(bin_path)])
    if result.success:
        return f"OK: esptool flash complete\n{result.output}"
    return f"ERROR: {result.output}"
