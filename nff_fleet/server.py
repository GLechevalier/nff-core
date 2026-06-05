from contextlib import asynccontextmanager

from fastmcp import FastMCP

from nff_fleet.broker.broker import start_broker, stop_broker
from nff_fleet.broker.router import start_router, stop_router
from nff_fleet.reaper import start_reaper, stop_reaper
from nff_fleet.config import settings
from nff_fleet import signing, db as _db


@asynccontextmanager
async def _lifespan(app):
    signing.load_key(settings.cmd_sign_key)
    await _db.init()
    await start_broker()
    await start_router()
    await start_reaper()
    await _db.set_agent_connected(True)
    yield
    await _db.set_agent_connected(False)
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
