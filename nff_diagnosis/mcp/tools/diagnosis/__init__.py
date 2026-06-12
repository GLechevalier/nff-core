from mcp.server.fastmcp import FastMCP

from nff_diagnosis.mcp.tools.diagnosis.ops import (
    diagnose_crash,
    diagnose_serial,
    ingest_crash,
    resolve_backtrace,
)

_TOOLS = [
    diagnose_serial,
    diagnose_crash,
    ingest_crash,
    resolve_backtrace,
]


def register(root: FastMCP) -> None:
    for fn in _TOOLS:
        root.add_tool(fn)
