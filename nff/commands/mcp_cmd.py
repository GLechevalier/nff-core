"""nff mcp — launch the MCP server (streamable-HTTP transport) or manage it."""

import asyncio

import click


@click.group("mcp", invoke_without_command=True)
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind address")
@click.option("--port", default=3010, type=int, show_default=True, help="Bind port")
@click.option("--stdio", is_flag=True,
              help="Serve MCP over stdio (for the Claude Code plugin) instead of HTTP.")
@click.pass_context
def mcp(ctx, host, port, stdio):
    """Start the nff MCP server (HTTP on host:port/mcp), or manage it.

    Run bare `nff mcp` to start it; use `stop`/`restart`/`logs` to manage the
    background server that `nff init` launched."""
    ctx.obj = {"host": host, "port": port}
    if ctx.invoked_subcommand is None:
        _start(host, port, stdio)


def _start(host, port, stdio=False):
    from nff import mcp_server
    from nff.tools import daemon

    # stdio binds no port, so it must run even when the HTTP daemon is already up
    # (the Claude Code plugin spawns one stdio process per session).
    if stdio:
        asyncio.run(mcp_server.run_stdio())
        return
    # `nff init` already starts the server in the background, so a manual `nff mcp`
    # would otherwise crash on the bound port. Bail out cleanly if it's already up.
    if daemon.is_running(host, port):
        click.echo(f"nff MCP server already running on http://{host}:{port}/mcp")
        return
    asyncio.run(mcp_server.run_server(host=host, port=port))


@mcp.command("stop")
def stop():
    """Stop the background MCP server (started by `nff init`)."""
    from nff.tools import daemon

    was_running = daemon.is_running()
    if daemon.stop():
        click.echo("nff MCP server stopped.")
    elif was_running:
        click.echo(
            f"nff MCP server is running but wasn't started by nff "
            f"(no pidfile at {daemon.pid_path()}) — stop it manually."
        )
    else:
        click.echo("nff MCP server is not running.")


@mcp.command("restart")
@click.pass_context
def restart(ctx):
    """Stop then start a fresh background MCP server."""
    from nff.tools import daemon

    host, port = ctx.obj["host"], ctx.obj["port"]
    if daemon.restart(host, port):
        click.echo(f"nff MCP server restarted on http://{host}:{port}/mcp")
    else:
        raise click.ClickException(f"failed to restart MCP server; see {daemon.log_path()}")


@mcp.command("logs")
@click.option("--lines", "-n", "lines", default=None, type=int, help="Show only the last N lines.")
def logs(lines):
    """Print the background MCP server's log."""
    from nff.tools import daemon

    path = daemon.log_path()
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        raise click.ClickException(f"no MCP log at {path} yet")
    if lines is not None:
        content = "\n".join(content.splitlines()[-lines:])
    click.echo(content)
