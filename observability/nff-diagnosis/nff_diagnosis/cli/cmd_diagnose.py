import json
import os
import sys
from pathlib import Path

import click

from nff_diagnosis.cli import load_crash
from nff_diagnosis.engine.diagnosis import DiagnosisEngine
from nff_diagnosis.resolution.addr2line import Addr2LineResolver
from nff_diagnosis.store.local import LocalELFStore


@click.command("diagnose")
@click.argument("crash_source", metavar="CRASH_JSON_OR_FILE")
@click.option("--api-key", default=None, envvar="ANTHROPIC_API_KEY")
@click.pass_context
def diagnose_cmd(ctx: click.Context, crash_source: str, api_key: str | None) -> None:
    """Resolve + diagnose a CrashRecord. Pass '-' to read stdin."""
    store_root: Path = ctx.obj["store_root"]
    try:
        crash = load_crash(crash_source)
        store = LocalELFStore(store_root)
        elf_path = store.get_elf_path(crash.build_id)
        resolver = Addr2LineResolver()
        resolved_bt, location = resolver.resolve(crash, elf_path)
        engine = DiagnosisEngine(api_key=api_key)
        diag = engine.diagnose(crash, resolved_bt, location)
        click.echo(diag.model_dump_json())
    except Exception as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(1)
