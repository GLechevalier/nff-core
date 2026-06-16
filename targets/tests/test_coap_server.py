"""CoAP actuator (insecure extra) — the Chain-7 surface.

The actuator's logic (link advertises ``if="actuator"``, PUT answers 2.04 Changed)
is unit-tested directly on every platform. The full UDP discover→PUT roundtrip via
a real aiocoap client is gated to non-Windows: aiocoap-over-loopback-UDP is flaky
on Windows + Python 3.14, but rock-solid on the Linux Pi/Docker demo target where
the server actually runs.
"""

from __future__ import annotations

import asyncio
import sys

import pytest
from conftest import free_udp_port, running

from nff_mock_targets.common.banners import LINUX

aiocoap = pytest.importorskip("aiocoap")

from nff_mock_targets.common.coap_server import CoapServer, _Actuator  # noqa: E402


async def test_actuator_advertises_if_attribute():
    ld = _Actuator().get_link_description()
    # coap_discover parses `;if="actuator"` from the CoRE link → Chain 7 root.
    assert ld.get("if") == "actuator"


async def test_actuator_put_returns_changed():
    resp = await _Actuator().render_put(aiocoap.Message(code=aiocoap.PUT, payload=b"on"))
    # coap_put declares success on a 2.0x response (2.04 Changed).
    assert resp.code == aiocoap.CHANGED


@pytest.mark.skipif(sys.platform == "win32", reason="aiocoap UDP loopback flaky on Windows")
async def test_discover_then_put_over_udp():
    port = free_udp_port()
    async with running(CoapServer(LINUX, "127.0.0.1", port)):
        ctx = await aiocoap.Context.create_client_context()
        try:
            disc = await ctx.request(
                aiocoap.Message(code=aiocoap.GET, uri=f"coap://127.0.0.1:{port}/.well-known/core")
            ).response
            assert 'if="actuator"' in disc.payload.decode()
            put = await ctx.request(
                aiocoap.Message(
                    code=aiocoap.PUT, uri=f"coap://127.0.0.1:{port}/actuator", payload=b"on"
                )
            ).response
            assert put.code == aiocoap.CHANGED
        finally:
            await ctx.shutdown()
        await asyncio.sleep(0)
