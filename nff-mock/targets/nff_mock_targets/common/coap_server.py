"""An unauthenticated CoAP actuator (aiocoap) — Chain 7.

``coap_discover`` GETs ``/.well-known/core`` and parses the CoRE link format;
the chain activates on a resource whose ``if`` attribute is ``actuator``.
``coap_put`` then PUTs to it and declares success on a 2.0x response. This server
advertises ``</actuator>;if="actuator"`` and answers PUT with ``2.04 Changed`` —
no DTLS, no token.

Requires the ``[insecure]`` extra (``aiocoap``). Imported lazily by the app.
"""

from __future__ import annotations

import asyncio
import logging

import aiocoap
import aiocoap.resource as resource

from nff_mock_targets.common.banners import Flavor

logger = logging.getLogger(__name__)


class _Actuator(resource.Resource):
    """A relay-style actuator that accepts unauthenticated reads and writes."""

    def __init__(self) -> None:
        super().__init__()
        self._state = b"off"

    def get_link_description(self) -> dict[str, str]:
        # Rendered into /.well-known/core as `</actuator>;if="actuator";rt="..."`.
        return {"if": "actuator", "rt": "nff.relay", "title": "actuator relay"}

    async def render_get(self, request: aiocoap.Message) -> aiocoap.Message:
        return aiocoap.Message(payload=self._state)

    async def render_put(self, request: aiocoap.Message) -> aiocoap.Message:
        self._state = request.payload or b"on"
        logger.info("coap PUT accepted (no auth): %r", self._state)
        return aiocoap.Message(code=aiocoap.CHANGED, payload=b"ok")


class CoapServer:
    def __init__(self, flavor: Flavor, bind: str, port: int) -> None:
        self.flavor = flavor
        self.bind = bind
        self.port = port

    async def serve(self) -> None:
        root = resource.Site()
        root.add_resource(
            (".well-known", "core"),
            resource.WKCResource(root.get_resources_as_linkheader),
        )
        root.add_resource(("actuator",), _Actuator())

        ctx = await aiocoap.Context.create_server_context(root, bind=(self.bind, self.port))
        logger.info("coap target listening on %s:%d/udp", self.bind, self.port)
        try:
            await asyncio.Event().wait()  # run until cancelled
        finally:
            await ctx.shutdown()
