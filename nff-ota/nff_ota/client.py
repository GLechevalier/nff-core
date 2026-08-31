from __future__ import annotations

import asyncio

from supabase import AsyncClient, acreate_client

from nff_ota.config import settings

# A direct override, used by tests to inject a mock. When set it wins over the
# per-loop registry, so get_client()/ensure_client() return it regardless of loop.
_client: AsyncClient | None = None

# Supabase's AsyncClient wraps an httpx connection pool bound to the event loop it
# was created on; it cannot be shared across loops without "attached to a different
# loop" errors. nff-ota runs TWO long-lived loops — the MCP server's (which also
# serves the webhook routes) and the background sweeps' (poller/reaper, in their own
# daemon-thread loop; see background.py). So we keep one client per running loop.
_clients: dict[asyncio.AbstractEventLoop, AsyncClient] = {}


async def init_client() -> AsyncClient:
    """Create (once) and return the Supabase client for the current event loop.

    Idempotent per loop: the streamable-http transport enters the MCP lifespan once
    per session (always the same loop), and the background thread calls this once on
    its own loop. Each loop gets its own client because a shared one would break with
    httpx loop-affinity errors. A test-injected override (``_client``) short-circuits.
    """
    if _client is not None:
        return _client
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        client = await acreate_client(settings.supabase_url, settings.supabase_key)
        _clients[loop] = client
    return client


async def ensure_client() -> AsyncClient:
    """Return the current loop's client, creating it on first use.

    Webhook handlers run on app-level Starlette routes that bypass the per-session MCP
    lifespan, so the client may not have been initialised yet. Calling this at handler
    entry guarantees a client without relying on lifespan ordering.
    """
    return await init_client()


async def close_client() -> None:
    """Drop the current loop's client (and any test override)."""
    global _client
    _client = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _clients.clear()
        return
    _clients.pop(loop, None)


def get_client() -> AsyncClient:
    if _client is not None:
        return _client
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as exc:
        raise RuntimeError("Supabase client not initialised. Call init_client() first.") from exc
    client = _clients.get(loop)
    if client is None:
        raise RuntimeError("Supabase client not initialised. Call init_client() first.")
    return client
