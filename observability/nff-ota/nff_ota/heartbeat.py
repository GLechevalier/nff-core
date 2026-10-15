"""Service liveness heartbeat (thread-driven).

Runs in a dedicated daemon thread, NOT as an asyncio task on the MCP server's
event loop. nff-ota serves over the official MCP SDK's streamable-http transport
(``mcp.server.fastmcp``), and in that setup lifespan-created asyncio background
tasks are only driven while the server is handling a request — between tool
calls the loop is idle and the task stalls. An async heartbeat would therefore
freeze whenever the server is quiet and the dashboard would show ota offline.

A plain thread + synchronous POST to PostgREST ticks independently of the
server's event loop, so liveness is reported every interval no matter what the
server is doing. (nff-fleet uses the standalone ``fastmcp`` library, whose loop
runs continuously, so it can keep its heartbeat as an asyncio task.)
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request

from nff_ota.config import settings

logger = logging.getLogger(__name__)

SERVICE_NAME = "nff-ota"
# Seconds between beats. Keep comfortably below the dashboard's stale window
# (~30s) so a couple of dropped beats don't flip the service to offline.
HEARTBEAT_INTERVAL = 10.0

_stop = threading.Event()
_thread: threading.Thread | None = None


def _beat_once() -> None:
    url = settings.supabase_url.rstrip("/") + "/rest/v1/rpc/service_heartbeat"
    body = json.dumps({"p_service": SERVICE_NAME, "p_version": None}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "apikey": settings.supabase_key,
            "Authorization": f"Bearer {settings.supabase_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()  # drain (PostgREST returns 204 No Content)


def _loop() -> None:
    # Beat immediately, then once per interval until stopped.
    while not _stop.is_set():
        try:
            _beat_once()
        except Exception as exc:  # never let a transient error kill the loop
            logger.warning("heartbeat error: %s", exc)
        _stop.wait(HEARTBEAT_INTERVAL)


def start() -> None:
    global _thread
    if not settings.supabase_url or not settings.supabase_key:
        logger.info("heartbeat disabled (Supabase not configured)")
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="nff-ota-heartbeat", daemon=True)
    _thread.start()
    logger.info(
        "heartbeat started (service=%s interval=%ss)", SERVICE_NAME, HEARTBEAT_INTERVAL
    )


async def stop() -> None:
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=2.0)
    logger.info("heartbeat stopped")
