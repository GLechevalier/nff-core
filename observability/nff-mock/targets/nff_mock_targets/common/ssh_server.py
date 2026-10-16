"""A weak-password SSH server (asyncssh) — the SSH leg of Chain 4.

``ssh_brute`` (pentester ``tools/credentials/brute.py``) declares success when
``asyncssh.connect(..., username, password)`` completes without raising. So all
this target needs is to accept password auth for a weak pair — the same
``root/root`` planted in the firmware image.

Requires the ``[insecure]`` extra (``asyncssh``). Imported lazily by the app so
the base install works without it.
"""

from __future__ import annotations

import asyncio
import logging

import asyncssh

from nff_mock_targets.common.banners import Flavor

logger = logging.getLogger(__name__)


class _AuthServer(asyncssh.SSHServer):
    def __init__(self, weak_logins: dict[str, str]) -> None:
        self._weak = weak_logins

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        self._peer = conn.get_extra_info("peername")

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        ok = self._weak.get(username) == password
        if ok:
            logger.info("ssh login OK %s:%s from %s", username, password, getattr(self, "_peer", None))
        return ok


async def _handle_session(process: asyncssh.SSHServerProcess) -> None:
    """Minimal canned shell so an interactive session also 'works'."""
    process.stdout.write("BusyBox v1.31.1 (nff-mock) built-in shell (ash)\r\n/ # ")
    try:
        async for line in process.stdin:
            cmd = line.strip()
            if cmd in ("exit", "quit", "logout"):
                break
            if cmd == "id":
                process.stdout.write("uid=0(root) gid=0(root) groups=0(root)\r\n")
            elif cmd == "whoami":
                process.stdout.write("root\r\n")
            else:
                process.stdout.write(f"sh: {cmd}: applet not found\r\n")
            process.stdout.write("/ # ")
    except asyncssh.BreakReceived:
        pass
    process.exit(0)


class SshServer:
    def __init__(self, flavor: Flavor, bind: str, port: int) -> None:
        self.flavor = flavor
        self.bind = bind
        self.port = port

    async def serve(self) -> None:
        # Ephemeral host key — regenerated each boot; fine for a throwaway target.
        host_key = asyncssh.generate_private_key("ssh-ed25519")
        weak = self.flavor.weak_logins
        server = await asyncssh.create_server(
            lambda: _AuthServer(weak),
            self.bind,
            self.port,
            server_host_keys=[host_key],
            process_factory=_handle_session,
            server_version="SSH-2.0-dropbear_2019.78",
        )
        logger.info("ssh target listening on %s:%d", self.bind, self.port)
        try:
            await server.wait_closed()
        finally:
            server.close()
