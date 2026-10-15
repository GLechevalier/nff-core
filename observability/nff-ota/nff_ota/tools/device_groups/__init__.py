from mcp.server.fastmcp import FastMCP

from nff_ota.tools.device_groups.ops import (
    add_devices_to_group,
    create_group,
    list_groups,
    remove_devices_from_group,
)

_TOOLS = [create_group, add_devices_to_group, remove_devices_from_group, list_groups]


def register(root: FastMCP) -> None:
    for fn in _TOOLS:
        root.add_tool(fn)
