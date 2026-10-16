"""SSH server (insecure extra) must accept a weak password and reject others."""

from __future__ import annotations

import pytest
from conftest import free_tcp_port, running

from nff_mock_targets.common.banners import LINUX

asyncssh = pytest.importorskip("asyncssh")

from nff_mock_targets.common.ssh_server import SshServer  # noqa: E402


async def test_weak_password_accepted():
    port = free_tcp_port()
    async with running(SshServer(LINUX, "127.0.0.1", port)):
        # ssh_brute declares success when connect() completes without raising.
        async with asyncssh.connect(
            "127.0.0.1", port=port, username="root", password="root", known_hosts=None
        ):
            pass


async def test_wrong_password_rejected():
    port = free_tcp_port()
    async with running(SshServer(LINUX, "127.0.0.1", port)):
        with pytest.raises(asyncssh.PermissionDenied):
            await asyncssh.connect(
                "127.0.0.1", port=port, username="root", password="wrong", known_hosts=None
            )
