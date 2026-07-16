"""nff flash — compile and upload a sketch to the device."""

import pathlib

import click
from rich.console import Console

from nff import config
from nff.tools import (
    arduino_lib,
    boards as boards_module,
    toolchain,
)

console = Console()


@click.command()
@click.argument("file", type=click.Path(exists=True))
@click.option("--board", default=None, help="Board: arduino-cli FQBN or PlatformIO board id")
@click.option("--port", default=None, help="Serial port")
@click.option("--baud", default=None, type=int)
@click.option("--manual-reset", is_flag=True)
def flash(file, board, port, baud, manual_reset):
    """Compile and flash a sketch to the device.

    FILE may be a .ino file or a sketch folder. To only check that a sketch
    builds (no board needed), use `nff compile` instead.
    """
    # Resolve board (FQBN for arduino backend, PlatformIO board id for pio backend)
    fqbn = board or toolchain.configured_board()
    if not fqbn:
        raise click.ClickException("No board — pass --board or run `nff init`")

    # Normalise the sketch into a compilable folder (handles .ino files and dirs).
    try:
        sketch_dir = toolchain.resolve_sketch_dir(source=pathlib.Path(file))
    except toolchain.ToolchainError as exc:
        raise click.ClickException(str(exc))

    # Resolve port (hardware path only)
    resolved_port = port or config.get_default_device().get("port") or ""
    if not resolved_port:
        detected = boards_module.find_device()
        if detected:
            resolved_port = detected.port
    if not resolved_port:
        raise click.ClickException("No port — pass --port or run `nff init`")

    # Fail early with actionable guidance if the backend's build tool is missing,
    # instead of the bare "Executable not found: pio" the stream layer would raise.
    try:
        toolchain.ensure_build_tool()
    except toolchain.ToolchainError as exc:
        raise click.ClickException(str(exc))

    if manual_reset:
        click.pause("Press Enter after manually resetting the board…")

    # Non-blocking: warn if a local nff-sdk-c checkout is newer than the synced
    # Arduino library, so "flash to test the fix" never silently builds old code.
    # Arduino-backend only — the pio backend materialises the SDK per-project.
    if toolchain.active_backend() == "arduino":
        warn = arduino_lib.local_sdk_newer_than_synced()
        if warn:
            console.print(f"[yellow]warning:[/yellow] {warn}")

    console.print("[bold]Compiling…[/bold]")
    rc = toolchain.stream_with_retry(
        lambda: toolchain.stream_compile(sketch_dir, fqbn), click.echo,
        recover=toolchain.package_recover(fqbn),
    )
    if rc != 0:
        raise click.ClickException("Compile failed")

    console.print("[bold]Uploading…[/bold]")
    rc = toolchain.stream_with_retry(
        lambda: toolchain.stream_upload(sketch_dir, fqbn, resolved_port),
        click.echo,
        backoff=(2.0, 4.0),
    )
    if rc != 0:
        raise click.ClickException("Upload failed")
    console.print("[green]Flash complete[/green]")

    # Record the freshly-built artifact so `nff status` can report it.
    artifacts = toolchain.discover_artifacts(sketch_dir, fqbn)
    if "elf" in artifacts:
        config.set_last_build(str(artifacts["elf"]), "elf", config.now_unix())
    else:
        for kind in ("merged_bin", "bin", "hex"):
            if kind in artifacts:
                config.set_last_build(str(artifacts[kind]), "bin", config.now_unix())
                break
