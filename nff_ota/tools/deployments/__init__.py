from mcp.server.fastmcp import FastMCP

from nff_ota.tools.deployments.ops import (
    abort_deployment,
    create_deployment,
    get_deployment_status,
    list_deployments,
    report_device_status,
    resume_deployment,
    start_deployment,
)

_TOOLS = [
    create_deployment,
    start_deployment,
    get_deployment_status,
    abort_deployment,
    resume_deployment,
    list_deployments,
    report_device_status,
]


def register(root: FastMCP) -> None:
    for fn in _TOOLS:
        root.add_tool(fn)
