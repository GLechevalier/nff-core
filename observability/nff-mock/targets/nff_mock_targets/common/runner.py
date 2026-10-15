"""Run a set of long-lived service coroutines together with graceful shutdown.

Mirrors the task-cancel + ``asyncio.gather(..., return_exceptions=True)`` pattern
used by the secure device in ``nff-mock/device/nff_mock/device.py``.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# A service is a coroutine factory that runs until cancelled.
Service = Callable[[], Awaitable[None]]


async def run_services(services: list[tuple[str, Service]]) -> None:
    """Start every service, run until a stop signal arrives, then cancel cleanly.

    ``services`` is a list of ``(label, coro_factory)``. Each factory is awaited as
    its own task; a service that raises is logged and does not bring down the others.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        logger.info("shutdown signal received")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, ValueError):
            # Windows / non-main-thread: fall back to KeyboardInterrupt handling.
            pass

    async def _guard(label: str, factory: Service) -> None:
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a target service must not kill the app
            logger.exception("service %s crashed", label)

    tasks = [asyncio.create_task(_guard(label, f), name=label) for label, f in services]
    logger.info("started %d services: %s", len(tasks), ", ".join(l for l, _ in services))

    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("all services stopped")
