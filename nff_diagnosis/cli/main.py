from pathlib import Path

import click
from dotenv import load_dotenv

# Prefer .env.dev (loaded first → wins), fall back to a plain .env. load_dotenv
# never overrides an already-set var, so the first call takes precedence.
load_dotenv(".env.dev")
load_dotenv()

from nff_diagnosis.cli.cmd_capture import capture_group
from nff_diagnosis.cli.cmd_diagnose import diagnose_cmd
from nff_diagnosis.cli.cmd_elf import elf_group
from nff_diagnosis.cli.cmd_fleet import fleet_group
from nff_diagnosis.cli.cmd_mcp import start_cmd
from nff_diagnosis.cli.cmd_resolve import resolve_cmd
from nff_diagnosis.cli.cmd_server import server_group
from nff_diagnosis.cli.cmd_view import view_group

_DEFAULT_STORE = Path.home() / ".nff-diagnosis" / "store"
_DEFAULT_DB = Path.home() / ".nff-diagnosis" / "crashes.db"


@click.group()
@click.option(
    "--store-root",
    default=str(_DEFAULT_STORE),
    show_default=True,
    type=click.Path(),
    envvar="NFF_STORE_ROOT",
)
@click.option(
    "--db",
    "db_path",
    default=str(_DEFAULT_DB),
    show_default=True,
    type=click.Path(),
    envvar="NFF_DB_PATH",
)
@click.pass_context
def cli(ctx: click.Context, store_root: str, db_path: str) -> None:
    """nff-diagnosis — ESP32 crash diagnosis tool."""
    ctx.ensure_object(dict)
    ctx.obj["store_root"] = Path(store_root)
    ctx.obj["db_path"] = Path(db_path)


cli.add_command(elf_group)
cli.add_command(capture_group)
cli.add_command(resolve_cmd)
cli.add_command(diagnose_cmd)
cli.add_command(start_cmd)
cli.add_command(server_group)
cli.add_command(view_group)
cli.add_command(fleet_group)
