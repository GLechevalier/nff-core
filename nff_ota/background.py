"""Background sweeps (thread-driven): the deployment poller and stuck-job reaper.

Both must run on their interval even while nff-ota is idle. Like the heartbeat, they
cannot live on the MCP server's event loop: the official MCP SDK's streamable-http
transport enters/exits the user ``lifespan`` per request, so a lifespan-scoped asyncio
task is created and cancelled on every call and never ticks between requests. That is
why the dashboard's "Push OTA" (which only writes a ``scheduled`` deployment row) used
to sit at 0% forever — nothing swept it.

Unlike the heartbeat (a synchronous urllib POST), the poller and reaper are async and
reach Supabase through the AsyncClient, which is bound to the loop it was created on.
So this thread runs its OWN event loop and creates its OWN client on it (init_client()
is per-loop). That client is independent of the MCP server's, so the per-request
lifespan teardown never touches it.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from nff_ota import poller, reaper
from nff_ota.client import init_client
from nff_ota.config import settings

logger = logging.getLogger(__name__)

_thread: threading.Thread | None = None
_loop: asyncio.AbstractEventLoop | None = None


async def _run() -> None:
    # Create this loop's own Supabase client before any sweep calls get_client().
    await init_client()
    tasks = [asyncio.create_task(reaper._loop())]
    if settings.deployment_poller_enabled:
        tasks.append(asyncio.create_task(poller._loop()))
    else:
        logger.info("poller disabled (OTA_DEPLOYMENT_POLLER_ENABLED=false)")
    await asyncio.gather(*tasks)


def _thread_main() -> None:
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(_run())
    except RuntimeError:
        pass  # loop stopped from another thread during shutdown
    except Exception as exc:  # never let an unexpected error escape the daemon thread
        logger.warning("background sweeps loop exited: %s", exc)
    finally:
        _loop.close()


def start() -> None:
    global _thread
    if not settings.supabase_url or not settings.supabase_key:
        logger.info("background sweeps disabled (Supabase not configured)")
        return
    _thread = threading.Thread(target=_thread_main, name="nff-ota-sweeps", daemon=True)
    _thread.start()
    logger.info(
        "background sweeps started (poller_interval=%ss reaper_interval=%ss)",
        settings.deployment_poller_interval, settings.reaper_interval,
    )


def stop() -> None:
    if _loop is not None:
        _loop.call_soon_threadsafe(_loop.stop)
    if _thread is not None:
        _thread.join(timeout=2.0)
    logger.info("background sweeps stopped")
