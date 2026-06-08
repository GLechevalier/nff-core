from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from mcp.server.fastmcp import FastMCP

from nff_ota import poller, reaper, webhooks
from nff_ota.client import close_client, init_client
from nff_ota.config import settings
from nff_ota.prompts import register_prompts
from nff_ota.resources import register_resources
from nff_ota.tools.artifacts import register as register_artifacts
from nff_ota.tools.deployments import register as register_deployments
from nff_ota.tools.device_groups import register as register_groups
from nff_ota.tools.devices import register as register_devices


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[None]:
    await init_client()
    reaper.start()
    poller.start()
    try:
        yield
    finally:
        await poller.stop()
        await reaper.stop()
        await close_client()


mcp = FastMCP(
    name="nff-ota",
    instructions=(
        "OTA firmware update management server. "
        "Upload artifacts, manage device authentication, create deployments, "
        "and track per-device update progress. "
        "Artifacts are immutable once uploaded — delete and re-upload to replace. "
        "Always verify device group membership before creating a deployment."
    ),
    log_level=settings.log_level,
    lifespan=lifespan,
    host=settings.mcp_host,
    port=settings.mcp_port,
)

register_artifacts(mcp)
register_devices(mcp)
register_deployments(mcp)
register_groups(mcp)
register_resources(mcp)
register_prompts(mcp)
webhooks.register(mcp)


def main() -> None:
    mcp.run(transport="streamable-http")
