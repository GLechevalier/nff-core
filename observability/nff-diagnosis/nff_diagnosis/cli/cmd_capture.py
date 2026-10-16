import sys
from pathlib import Path

import click

from nff_diagnosis.capture.nff_bridge import NffBridge
from nff_diagnosis.capture.parser import ESP32PanicParser
from nff_diagnosis.schemas import CrashRecord
from nff_diagnosis.store.local import LocalELFStore


@click.group("capture")
def capture_group() -> None:
    """Capture and parse ESP32 panic output."""


@capture_group.command("flash-and-capture")
@click.option("--elf", "elf_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--port", required=True, help="Serial port, e.g. COM3 or /dev/ttyUSB0")
@click.option("--board", required=True, help="FQBN, e.g. esp32:esp32:esp32")
@click.option("--customer", required=True)
@click.option("--project", required=True)
@click.option("--firmware-version", required=True)
@click.option("--duration-ms", default=8000, show_default=True)
@click.option("--out", "out_path", default=None, type=click.Path(), help="Write JSON to file instead of stdout")
@click.pass_context
def flash_and_capture(
    ctx: click.Context,
    elf_path: str,
    port: str,
    board: str,
    customer: str,
    project: str,
    firmware_version: str,
    duration_ms: int,
    out_path: str | None,
) -> None:
    """Flash firmware, capture serial panic output, emit CrashRecord JSON."""
    store_root: Path = ctx.obj["store_root"]
    store = LocalELFStore(store_root)
    bridge = NffBridge()
    parser = ESP32PanicParser()
    try:
        elf_bytes = Path(elf_path).read_bytes()
        record = store.put(
            elf_bytes=elf_bytes,
            customer=customer,
            project=project,
            firmware_version=firmware_version,
        )
        bridge.flash(Path(elf_path), port=port, board=board)
        raw = bridge.serial_read(port=port, duration_ms=duration_ms)
        fields = parser.parse(raw)
        fields["build_id"] = record.build_id
        fields["raw"] = raw
        crash = CrashRecord(**fields)
        output = crash.model_dump_json()
        if out_path:
            Path(out_path).write_text(output, encoding="utf-8")
        else:
            click.echo(output)
    except Exception as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(1)


@capture_group.command("from-file")
@click.option("--raw", "raw_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--build-id", required=True)
@click.option("--device-id", default=None)
@click.option("--out", "out_path", default=None, type=click.Path())
def capture_from_file(
    raw_path: str,
    build_id: str,
    device_id: str | None,
    out_path: str | None,
) -> None:
    """Parse a saved panic dump file, emit CrashRecord JSON. No hardware needed."""
    parser = ESP32PanicParser()
    try:
        raw = Path(raw_path).read_text(encoding="utf-8", errors="replace")
        fields = parser.parse(raw)
        fields["build_id"] = build_id
        fields["raw"] = raw
        if device_id:
            fields["device_id"] = device_id
        crash = CrashRecord(**fields)
        output = crash.model_dump_json()
        if out_path:
            Path(out_path).write_text(output, encoding="utf-8")
        else:
            click.echo(output)
    except Exception as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(1)
