from mcp.server.fastmcp import FastMCP

from nff_ota.tools.source.ops import get_firmware_source, submit_firmware_fix

_TOOLS = [get_firmware_source, submit_firmware_fix]


def register(root: FastMCP) -> None:
    for fn in _TOOLS:
        root.add_tool(fn)
