import sys
from pathlib import Path

import click

from nff_diagnosis.store.local import LocalELFStore


@click.group("elf")
def elf_group() -> None:
    """Manage ELF binaries in the store."""


@elf_group.command("upload")
@click.option("--elf", "elf_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--customer", required=True)
@click.option("--project", required=True)
@click.option("--firmware-version", required=True)
@click.option("--target", default="esp32", show_default=True)
@click.option("--toolchain", default="", help="Toolchain/addr2line version string")
@click.pass_context
def elf_upload(
    ctx: click.Context,
    elf_path: str,
    customer: str,
    project: str,
    firmware_version: str,
    target: str,
    toolchain: str,
) -> None:
    """Upload an ELF binary to the local store."""
    store_root: Path = ctx.obj["store_root"]
    store = LocalELFStore(store_root)
    try:
        data = Path(elf_path).read_bytes()
        record = store.put(
            elf_bytes=data,
            customer=customer,
            project=project,
            firmware_version=firmware_version,
            target=target,
            toolchain=toolchain,
        )
        click.echo(record.model_dump_json())
    except Exception as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(1)


@elf_group.command("show")
@click.argument("build_id")
@click.pass_context
def elf_show(ctx: click.Context, build_id: str) -> None:
    """Show the ELFRecord for a given build_id."""
    store_root: Path = ctx.obj["store_root"]
    store = LocalELFStore(store_root)
    record = store.get_record(build_id)
    if record is None:
        click.echo(f"ERROR: build_id not found: {build_id}", err=True)
        sys.exit(1)
    click.echo(record.model_dump_json())
