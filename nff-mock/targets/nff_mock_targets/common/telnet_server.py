"""A weak-credential telnet server → fake BusyBox shell.

Speaks the exact handshake ``telnet_brute`` expects (pentester
``tools/credentials/brute.py::_try_telnet``):

  server: ``...login: ``      ← contains "login:"
  client: ``<username>\n``
  server: ``Password: ``       ← contains "Password:"
  client: ``<password>\n``
  server: shell prompt         ← on success, contains "$" (tool's success_prompt)

So a weak pair lands the tool in ``valid_credentials`` and Chain 1 goes active.
"""

from __future__ import annotations

import asyncio
import logging

from nff_mock_targets.common.banners import Flavor

logger = logging.getLogger(__name__)


async def _readline(reader: asyncio.StreamReader, limit: int = 256) -> str:
    data = await reader.read(limit)
    return data.decode(errors="replace").strip("\r\n").strip()


class TelnetServer:
    def __init__(self, flavor: Flavor, bind: str, port: int) -> None:
        self.flavor = flavor
        self.bind = bind
        self.port = port

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            writer.write(self.flavor.telnet_banner.encode())
            await writer.drain()
            username = await _readline(reader)

            writer.write(b"Password: ")
            await writer.drain()
            password = await _readline(reader)

            if self.flavor.weak_logins.get(username) == password:
                logger.info("telnet login OK %s:%s from %s", username, password, peer)
                writer.write(self.flavor.telnet_shell_prompt.encode())
                await writer.drain()
                await self._shell(reader, writer)
            else:
                writer.write(b"\r\nLogin incorrect\r\n")
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            with _closing(writer):
                pass

    async def _shell(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """A tiny canned shell — enough to look real once a brute-forcer is in."""
        while True:
            line = await _readline(reader)
            if not line or line in ("exit", "quit", "logout"):
                break
            if line == "id":
                writer.write(b"uid=0(root) gid=0(root) groups=0(root)\r\n")
            elif line in ("whoami",):
                writer.write(b"root\r\n")
            elif line.startswith("cat ") and "passwd" in line:
                writer.write(b"root:x:0:0:root:/root:/bin/sh\r\n")
            else:
                writer.write(f"sh: {line}: applet not found\r\n".encode())
            writer.write(self.flavor.telnet_shell_prompt.encode())
            await writer.drain()

    async def serve(self) -> None:
        server = await asyncio.start_server(self._handle, self.bind, self.port)
        addr = ", ".join(str(s.getsockname()) for s in server.sockets)
        logger.info("telnet target listening on %s", addr)
        async with server:
            await server.serve_forever()


class _closing:
    """Best-effort writer close that never raises in a finally block."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        try:
            self.writer.close()
        except Exception:  # noqa: BLE001
            pass
        return False
