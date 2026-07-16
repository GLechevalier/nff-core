"""nff compile — compile a sketch only (no upload, no port required)."""

import pathlib

import click
from rich.console import Console

from nff import config
from nff.tools import toolchain

console = Console()


@click.command(name="compile")
@click.argument("file", type=click.Path(exists=True))
@click.option("--board", default=None,
              help="Board: arduino-cli FQBN or PlatformIO board id (defaults to configured board)")
@click.option("--json", "as_json", is_flag=True, help="Emit the raw JSON result")
def compile_cmd(file, board, as_json):
    """Compile a sketch and report what was produced — never uploads.

    FILE may be a .ino/.cpp file or a sketch folder. No board connection is needed,
    so this is the fast way to check that a sketch builds.
    """
    fqbn = board or toolchain.configured_board()
    if not fqbn:
        raise click.ClickException("No board — pass --board or run `nff init`")

    try:
        result = toolchain.compile_only(fqbn, source=pathlib.Path(file))
    except toolchain.ToolchainError as exc:
        raise click.ClickException(str(exc))

    # Record the artifact so `nff status` can report the last build.
    if result.ok:
        artifact = result.elf or result.image
        if artifact:
            kind = "elf" if result.elf else "bin"
            config.set_last_build(str(artifact), kind, config.now_unix())

    if as_json:
        import json
        click.echo(json.dumps(result.to_dict(), indent=2))
        if not result.ok:
            raise SystemExit(1)
        return

    if not result.ok:
        console.print("[red]Compile failed[/red]")
        for line in (result.errors or [result.output]):
            click.echo(line)
        raise SystemExit(1)

    console.print(f"[green]Compile succeeded[/green] ({fqbn})")
    size = toolchain._extract_size(result.output)
    if size:
        console.print(size)
    if result.elf:
        console.print(f"elf:   {result.elf}")
    if result.image and result.image != result.elf:
        console.print(f"image: {result.image}")
