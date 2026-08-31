import click


@click.command("start")
@click.option("--host", default=None, help="Bind host (overrides NFF_DIAG_MCP_HOST).")
@click.option("--port", default=None, type=int, help="Bind port (overrides NFF_DIAG_MCP_PORT).")
@click.pass_context
def start_cmd(ctx: click.Context, host: str | None, port: int | None) -> None:
    """Start the nff-diagnosis MCP server (streamable-http, default port 3005)."""
    from nff_diagnosis.mcp.config import settings

    # Honor the top-level --store-root/--db so the MCP server shares the same ELF
    # store + crash DB as the other subcommands (and the REST server).
    if ctx.obj:
        if ctx.obj.get("store_root") is not None:
            settings.store_root = ctx.obj["store_root"]
        if ctx.obj.get("db_path") is not None:
            settings.db_path = ctx.obj["db_path"]

    # Import after settings are patched: building the FastMCP instance reads the
    # transport host/port, and the deps singletons read store_root/db_path lazily.
    from nff_diagnosis.mcp import server as mcp_server

    if host is not None:
        mcp_server.mcp.settings.host = host
    if port is not None:
        mcp_server.mcp.settings.port = port

    mcp_server.main()
