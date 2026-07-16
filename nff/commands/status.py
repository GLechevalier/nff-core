"""nff status — a read-only snapshot of the bench."""

import click

from nff import config
from nff.tools import boards, daemon, toolchain


@click.command()
def status():
    """Show a snapshot of the bench: backend, board, MCP server, auth, last build.

    Unlike `nff doctor` (a pass/fail health check), this just reports current state
    and always exits 0.
    """
    # Active build backend.
    click.echo(f"  build backend : {toolchain.active_backend()}")

    # Detected board (first recognised device, if any).
    devices = boards.list_devices()
    if devices:
        d = devices[0]
        click.echo(f"  board         : {d.board} on {d.port}")
    else:
        click.echo("  board         : none detected")

    # MCP server up/down + URL.
    if daemon.is_running():
        click.echo(
            f"  mcp server    : up    (http://{daemon.DEFAULT_HOST}:{daemon.DEFAULT_PORT}/mcp)"
        )
    else:
        click.echo("  mcp server    : down  (run `nff mcp` to start)")

    # Auth state: signed in, offline (local mode), or signed out.
    if config.get_diagnosis_config().get("access_token"):
        click.echo("  auth          : signed in")
    elif config.is_offline():
        click.echo("  auth          : offline (local mode)")
    else:
        click.echo("  auth          : signed out")

    # Last successful build artifact (recorded by compile/flash).
    last = config.get_last_build()
    if last and last.get("path"):
        click.echo(
            f"  last build    : {last['path']} "
            f"({last.get('kind', '?')}, {_format_age(last.get('built_at'))})"
        )
    else:
        click.echo("  last build    : none yet")


def _format_age(built_at) -> str:
    """Render a unix timestamp as a coarse 'N ago' string for the snapshot."""
    if not built_at:
        return "unknown"
    secs = config.now_unix() - int(built_at)
    if secs < 0:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"
