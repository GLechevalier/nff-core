import json
import sys
from pathlib import Path

import click

from nff_diagnosis.cli import load_crash
from nff_diagnosis.resolution.addr2line import Addr2LineResolver
from nff_diagnosis.store.local import LocalELFStore


@click.command("resolve")
@click.argument("crash_source", metavar="CRASH_JSON_OR_FILE")
@click.pass_context
def resolve_cmd(ctx: click.Context, crash_source: str) -> None:
    """Resolve backtrace addresses in a CrashRecord. Pass '-' to read stdin."""
    store_root: Path = ctx.obj["store_root"]
    try:
        crash = load_crash(crash_source)
        store = LocalELFStore(store_root)
        elf_path = store.get_elf_path(crash.build_id)
        resolver = Addr2LineResolver()
        resolved_bt, location = resolver.resolve(crash, elf_path)
        output = {
            "crash": json.loads(crash.model_dump_json()),
            "resolved_backtrace": [f.model_dump() for f in resolved_bt],
            "location": location,
        }
        click.echo(json.dumps(output))
    except Exception as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(1)
