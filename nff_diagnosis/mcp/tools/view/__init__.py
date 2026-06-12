from mcp.server.fastmcp import FastMCP

from nff_diagnosis.mcp.tools.view.ops import get_crash, get_diagnosis, list_crashes

_TOOLS = [list_crashes, get_crash, get_diagnosis]


def register(root: FastMCP) -> None:
    for fn in _TOOLS:
        root.add_tool(fn)
