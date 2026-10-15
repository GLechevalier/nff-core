from mcp.server.fastmcp import FastMCP

from nff_ota.tools.devices.ops import (
    accept_device,
    decommission_device,
    list_devices,
    preauthorize_device,
    register_device,
    reject_device,
)

_TOOLS = [
    register_device,
    accept_device,
    reject_device,
    preauthorize_device,
    list_devices,
    decommission_device,
]


def register(root: FastMCP) -> None:
    for fn in _TOOLS:
        root.add_tool(fn)
