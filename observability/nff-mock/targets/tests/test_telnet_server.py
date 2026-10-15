"""Telnet handshake must match what telnet_brute drives, and leak a '$' prompt."""

from __future__ import annotations

import asyncio

from conftest import free_tcp_port, running

from nff_mock_targets.common.banners import LINUX
from nff_mock_targets.common.telnet_server import TelnetServer


async def _login(port: int, username: str, password: str) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await asyncio.wait_for(reader.read(128), timeout=2)  # login banner
    writer.write((username + "\n").encode())
    await writer.drain()
    await asyncio.wait_for(reader.read(128), timeout=2)  # "Password: "
    writer.write((password + "\n").encode())
    await writer.drain()
    resp = await asyncio.wait_for(reader.read(512), timeout=2)
    writer.close()
    return resp


async def test_weak_cred_yields_dollar_prompt():
    port = free_tcp_port()
    async with running(TelnetServer(LINUX, "127.0.0.1", port)):
        resp = await _login(port, "root", "root")
    # telnet_brute declares success when its success_prompt ("$") is in the reply.
    assert b"$" in resp


async def test_bad_cred_rejected():
    port = free_tcp_port()
    async with running(TelnetServer(LINUX, "127.0.0.1", port)):
        resp = await _login(port, "root", "nope")
    assert b"$" not in resp
    assert b"incorrect" in resp.lower()
