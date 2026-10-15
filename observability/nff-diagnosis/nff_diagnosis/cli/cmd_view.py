import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from nff_diagnosis.storage.sqlite import SQLiteStorage


@click.group("view")
def view_group() -> None:
    """Read crashes and diagnoses from storage."""


@view_group.command("crash")
@click.argument("crash_id")
@click.option("--pretty", is_flag=True)
@click.pass_context
def view_crash(ctx: click.Context, crash_id: str, pretty: bool) -> None:
    """Print a CrashRecord from storage."""
    db_path: Path = ctx.obj["db_path"]
    storage = SQLiteStorage(db_path)
    record = storage.get_crash(crash_id)
    if record is None:
        click.echo(f"ERROR: crash not found: {crash_id}", err=True)
        sys.exit(1)
    if pretty:
        console = Console()
        console.print(record.model_dump())
    else:
        click.echo(record.model_dump_json())


@view_group.command("diag")
@click.argument("crash_id")
@click.option("--pretty", is_flag=True)
@click.pass_context
def view_diag(ctx: click.Context, crash_id: str, pretty: bool) -> None:
    """Print a DiagnosisOutput from storage."""
    db_path: Path = ctx.obj["db_path"]
    storage = SQLiteStorage(db_path)
    diag = storage.get_diagnosis(crash_id)
    if diag is None:
        click.echo(f"ERROR: diagnosis not found for crash: {crash_id}", err=True)
        sys.exit(1)
    if pretty:
        console = Console()
        console.print(f"[bold]crash_id[/bold]   : {diag.crash_id}")
        console.print(f"[bold]class[/bold]      : {diag.crash_class}   (confidence: {diag.confidence:.2f})")
        console.print(f"[bold]root_cause[/bold] : {diag.root_cause}")
        loc = diag.location
        if loc:
            console.print(f"[bold]location[/bold]   : {loc.get('function')}  {loc.get('file')}:{loc.get('line')}")
        if diag.resolved_backtrace:
            table = Table(title="Backtrace", show_header=True)
            table.add_column("#", style="dim")
            table.add_column("PC")
            table.add_column("Function")
            table.add_column("File:Line")
            for i, frame in enumerate(diag.resolved_backtrace):
                file_line = f"{frame.file}:{frame.line}" if frame.file else "??"
                table.add_row(str(i), frame.pc, frame.function or "??", file_line)
            console.print(table)
    else:
        click.echo(diag.model_dump_json())


@view_group.command("list")
@click.option("--limit", default=50, show_default=True)
@click.pass_context
def view_list(ctx: click.Context, limit: int) -> None:
    """List recent crashes."""
    db_path: Path = ctx.obj["db_path"]
    storage = SQLiteStorage(db_path)
    crashes = storage.list_crashes(limit=limit)
    if not crashes:
        click.echo("[]")
        return
    for c in crashes:
        click.echo(f"{c.crash_id}\t{c.panic_type}\t{c.received_at.isoformat()}")
