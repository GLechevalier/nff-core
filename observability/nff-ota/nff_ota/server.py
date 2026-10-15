from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from mcp.server.fastmcp import FastMCP

from nff_ota import background, heartbeat, webhooks
from nff_ota.client import init_client
from nff_ota.config import settings
from nff_ota.prompts import register_prompts
from nff_ota.resources import register_resources
from nff_ota.tools.artifacts import register as register_artifacts
from nff_ota.tools.deployments import register as register_deployments
from nff_ota.tools.device_groups import register as register_groups
from nff_ota.tools.devices import register as register_devices
from nff_ota.tools.source import register as register_source


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[None]:
    # The streamable-http transport enters this lifespan once per MCP session, not once
    # per process. init_client() is idempotent so it creates this loop's Supabase client
    # on first session and reuses it thereafter. We deliberately do NOT close_client() on
    # session exit: the client is loop-scoped and shared with the app-level webhook
    # routes (which never enter this lifespan), so tearing it down here would null a
    # client other sessions and webhooks still depend on.
    #
    # The poller and reaper are NOT started here: this lifespan is entered/exited per
    # request, so a sweep task started here would be cancelled the moment the request
    # ends and never tick while idle. They run as process-level background sweeps on
    # their own loop instead — started in main(). See nff_ota/background.py.
    await init_client()
    yield


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
register_source(mcp)
register_resources(mcp)
register_prompts(mcp)
webhooks.register(mcp)


def main() -> None:
    # The heartbeat is started at the process level, NOT inside the MCP lifespan:
    # this server's streamable-http transport enters/exits `lifespan` per request,
    # so a lifespan-scoped heartbeat would be started and immediately stopped on
    # every call and never tick while idle. A process-level daemon thread beats
    # for the whole process lifetime instead, independent of the SDK's lifespan.
    heartbeat.start()
    # Same reasoning for the deployment poller and stuck-job reaper: they must sweep on
    # their interval while the server is idle, but a lifespan-scoped task is torn down
    # per request. background.start() runs them on a dedicated daemon-thread event loop
    # (with its own loop-bound Supabase client) for the whole process lifetime.
    background.start()
    mcp.run(transport="streamable-http")
