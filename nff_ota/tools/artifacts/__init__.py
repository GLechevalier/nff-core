from mcp.server.fastmcp import FastMCP

from nff_ota.tools.artifacts.ops import delete_artifact, list_artifacts, upload_artifact

_TOOLS = [upload_artifact, list_artifacts, delete_artifact]


def register(root: FastMCP) -> None:
    for fn in _TOOLS:
        root.add_tool(fn)
