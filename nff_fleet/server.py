from contextlib import asynccontextmanager

from fastmcp import FastMCP

from nff_fleet.broker.broker import start_broker, stop_broker
from nff_fleet.broker.router import start_router, stop_router
from nff_fleet.reaper import start_reaper, stop_reaper
from nff_fleet.enroll_worker import start_enroll_worker, stop_enroll_worker
from nff_fleet.heartbeat import start_heartbeat, stop_heartbeat
from nff_fleet.config import settings
from nff_fleet import db as _db
from nff_fleet import signing as _signing


@asynccontextmanager
async def _lifespan(app):
    # Per-project signing keys are lazy-loaded on first use (signing._load); no global
    # key to preload here. The fleet rollover-signing key (used to authenticate claim rollovers)
    # is created once if absent so its verify-key can be baked into bootstrap credentials.
    _signing.ensure_rollover_key()
    await _db.init()
    await start_broker()
    await start_router()
    await start_reaper()
    await start_enroll_worker()
    await start_heartbeat()
    await _db.set_agent_connected(True)
    yield
    await _db.set_agent_connected(False)
    await stop_heartbeat()
    await stop_enroll_worker()
    await stop_reaper()
    await stop_router()
    await stop_broker()
    await _db.shutdown()


mcp = FastMCP("nff-fleet", lifespan=_lifespan)

# Register tools
from nff_fleet.tools.devices import fleet_list_devices, fleet_ping, fleet_reboot
from nff_fleet.tools.ota import fleet_store_elf
from nff_fleet.tools.crash import fleet_get_crash, fleet_resolve_backtrace
from nff_fleet.tools.diag import fleet_get_diag

mcp.tool()(fleet_list_devices)
mcp.tool()(fleet_ping)
mcp.tool()(fleet_reboot)
mcp.tool()(fleet_store_elf)
mcp.tool()(fleet_get_crash)
mcp.tool()(fleet_resolve_backtrace)
mcp.tool()(fleet_get_diag)

# Internal service-to-service control surface (nff-ota calls this to deliver commands).
from nff_fleet import control as _control  # noqa: E402  (registers @mcp.custom_route)

_control.register(mcp)
