"""Tests for the wired-onboarding WS↔TCP bridge (nff_fleet.link_bridge).

The bridge is the fleet-side half of the browser WiredLink: an authenticated WebSocket whose
binary frames are spliced byte-for-byte into a TCP connection — to the in-process broker by
default, or to the destination SIGNED INTO the token (OTA artifact host). These tests run the
real server against local TCP echo stand-ins.
"""

import asyncio
import base64
import hashlib
import hmac
import socket
import time

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from nff_fleet.config import settings
from nff_fleet.link_bridge import _token_dst, start_link_bridge, stop_link_bridge


def _mint(project_id: str, exp_offset_s: int = 300, secret: str | None = None,
          dst: str | None = None) -> str:
    """Mirror api/_link-token.ts: 3-part legacy (dst None) or 4-part with signed dst."""
    exp = int(time.time()) + exp_offset_s
    key = (secret or settings.mcp_secret).encode()
    if dst is None:
        sig = hmac.new(key, f"{project_id}.{exp}".encode(), hashlib.sha256).hexdigest()
        return f"{project_id}.{exp}.{sig}"
    sig = hmac.new(key, f"{project_id}.{exp}.{dst}".encode(), hashlib.sha256).hexdigest()
    dst_b64 = base64.urlsafe_b64encode(dst.encode()).decode().rstrip("=")
    return f"{project_id}.{exp}.{dst_b64}.{sig}"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _echo_server():
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        while data := await reader.read(4096):
            writer.write(data)
            await writer.drain()
        writer.close()
    return await asyncio.start_server(echo, "127.0.0.1", 0)


def test_token_forms():
    assert _token_dst(_mint("proj-1")) == ("proj-1", "")                       # legacy 3-part
    assert _token_dst(_mint("proj-1", dst="")) == ("proj-1", "")               # explicit broker
    assert _token_dst(_mint("p", dst="db.supabase.co:443")) == ("p", "db.supabase.co:443")
    assert _token_dst(_mint("p", exp_offset_s=-10, dst="x:1")) is None         # expired
    assert _token_dst(_mint("p", secret="wrong", dst="x:1")) is None           # bad signature
    # Tampered dst: re-encode a different destination without re-signing.
    good = _mint("p", dst="db.supabase.co:443").split(".")
    good[2] = base64.urlsafe_b64encode(b"evil.example.com:443").decode().rstrip("=")
    assert _token_dst(".".join(good)) is None
    assert _token_dst("garbage") is None
    assert _token_dst("") is None


def test_splices_to_broker_by_default(monkeypatch):
    async def scenario():
        echo_srv = await _echo_server()
        monkeypatch.setattr(settings, "mqtt_port", echo_srv.sockets[0].getsockname()[1])
        monkeypatch.setattr(settings, "link_port", _free_port())
        await start_link_bridge()
        try:
            async with connect(f"ws://127.0.0.1:{settings.link_port}/link?token={_mint('p')}") as ws:
                await ws.send(b"\x16\x03\x01hello-tls")
                assert await asyncio.wait_for(ws.recv(), timeout=5) == b"\x16\x03\x01hello-tls"
        finally:
            await stop_link_bridge()
            echo_srv.close()
            await echo_srv.wait_closed()

    asyncio.run(scenario())


def test_splices_to_signed_dst(monkeypatch):
    async def scenario():
        # A second echo server plays the OTA artifact host; mqtt_port points elsewhere on purpose.
        artifact_srv = await _echo_server()
        artifact_port = artifact_srv.sockets[0].getsockname()[1]
        monkeypatch.setattr(settings, "mqtt_port", _free_port())  # NOT listening — must not be used
        monkeypatch.setattr(settings, "link_port", _free_port())
        await start_link_bridge()
        try:
            tok = _mint("p", dst=f"127.0.0.1:{artifact_port}")
            async with connect(f"ws://127.0.0.1:{settings.link_port}/link?token={tok}") as ws:
                await ws.send(b"GET /artifact HTTP/1.1\r\n\r\n")
                assert await asyncio.wait_for(ws.recv(), timeout=5) == b"GET /artifact HTTP/1.1\r\n\r\n"
        finally:
            await stop_link_bridge()
            artifact_srv.close()
            await artifact_srv.wait_closed()

    asyncio.run(scenario())


def test_rejects_bad_token(monkeypatch):
    async def scenario():
        monkeypatch.setattr(settings, "link_port", _free_port())
        await start_link_bridge()
        try:
            async with connect(f"ws://127.0.0.1:{settings.link_port}/link?token=bogus") as ws:
                with pytest.raises(ConnectionClosed) as exc:
                    await asyncio.wait_for(ws.recv(), timeout=5)
                assert exc.value.rcvd is not None and exc.value.rcvd.code == 4401
        finally:
            await stop_link_bridge()

    asyncio.run(scenario())
