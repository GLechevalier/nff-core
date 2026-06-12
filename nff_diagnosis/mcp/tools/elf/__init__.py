from mcp.server.fastmcp import FastMCP

from nff_diagnosis.mcp.tools.elf.ops import show_elf, upload_elf

_TOOLS = [upload_elf, show_elf]


def register(root: FastMCP) -> None:
    for fn in _TOOLS:
        root.add_tool(fn)
