"""Shared test helpers: ephemeral ports + a 'run this service briefly' context."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import sys

# aiomqtt/paho (and aiocoap) need a SelectorEventLoop — the default on Linux (the
# Pi/Docker demo target) but NOT on Windows, whose ProactorEventLoop lacks
# add_reader/add_writer. Match the production loop so the tests exercise the real
# client integration on every platform.
if sys.platform == "win32":
    with contextlib.suppress(Exception):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def free_tcp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.asynccontextmanager
async def running(server):
    """Start ``server.serve()`` as a task, wait for it to bind, then cancel on exit."""
    task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.3)
    try:
        yield server
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
