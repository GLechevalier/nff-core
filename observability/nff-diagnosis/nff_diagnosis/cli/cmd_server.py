import sys
from pathlib import Path

import click
import uvicorn

from nff_diagnosis.server.app import create_app


@click.command("start")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8080, show_default=True)
@click.option("--api-key", default=None, envvar="ANTHROPIC_API_KEY")
@click.option("--model", default="claude-sonnet-4-6", show_default=True)
@click.pass_context
def server_start(
    ctx: click.Context,
    host: str,
    port: int,
    api_key: str | None,
    model: str,
) -> None:
    """Start the nff-diagnosis HTTP server."""
    store_root: Path = ctx.obj["store_root"]
    db_path: Path = ctx.obj["db_path"]
    try:
        app = create_app(store_root=store_root, db_path=db_path, api_key=api_key, model=model)
        uvicorn.run(app, host=host, port=port)
    except Exception as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(1)


@click.group("server")
def server_group() -> None:
    """Run the HTTP ingestion server."""


server_group.add_command(server_start)
