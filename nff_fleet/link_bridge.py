"""WebSocket↔TCP splice for wired (USB/PPPoS) onboarding.

The dashboard's WiredLink (browser tab) terminates a freshly-flashed device's PPPoS link over
Web Serial and needs somewhere to send the device's single TCP flow — its mTLS-MQTT session with
the fleet broker. This bridge is that somewhere:

    ws://<fleet>:{link_port}/link?token=<project_id>.<exp>.<hmac>
        <->  tcp 127.0.0.1:{mqtt_port}  (the in-process broker)

Auth: the token is minted by the dashboard backend (api/_link-token.ts) as
HMAC-SHA256(mcp_secret, "<project_id>.<exp>") — the same shared secret as the internal control
surface, but only the derived, expiring signature ever reaches the browser. The bridge moves
opaque TLS bytes: the device still authenticates itself to the broker with its own client cert,
so a leaked URL cannot impersonate a device — it can only open a socket that the broker's mTLS
handshake will reject.

One WS connection == one device TCP flow (the browser opens a new one per MQTT reconnect).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import time
from urllib.parse import parse_qs, urlparse

import websockets.http11
from websockets.asyncio.server import serve

from nff_fleet.config import settings

logger = logging.getLogger(__name__)

# websockets caps a single handshake line at 8192 bytes (MAX_LINE_LENGTH). The browser's WS upgrade
# request can exceed that — cookies for the localhost origin (Supabase auth) are NOT port-scoped, so
# they ride along to :3008 and a multi-KB auth cookie blows the request line. Raise the cap so a
# large-but-legitimate handshake isn't rejected (the connection is still gated by the signed token).
websockets.http11.MAX_LINE_LENGTH = 65536

_server = None

# A wedged flow shouldn't pin a socket forever; the browser reconnects if the device retries.
_IDLE_TIMEOUT_S = 300


def _token_dst(token: str) -> tuple[str, str] | None:
    """Validate a link token; returns (project_id, dst) or None.

    4-part form: <project>.<exp>.<b64url(dst)>.<hmac over "project.exp.dst"> — the signature
    covers the destination, so the authorization decision stays with the dashboard API that
    minted the token (this bridge dials whatever a VALID token names, nothing else).
    3-part legacy form (no dst) still verifies and means the broker.
    """
    parts = token.split(".")
    if len(parts) == 3:
        project_id, exp_s, sig = parts
        dst = ""
    elif len(parts) == 4:
        project_id, exp_s, dst_b64, sig = parts
        try:
            dst = base64.urlsafe_b64decode(dst_b64 + "=" * (-len(dst_b64) % 4)).decode()
        except (ValueError, UnicodeDecodeError):
            return None
    else:
        return None
    try:
        if int(exp_s) < time.time():
            return None
    except ValueError:
        return None
    want = hmac.new(
        settings.mcp_secret.encode(),
        (f"{project_id}.{exp_s}.{dst}" if len(parts) == 4 else f"{project_id}.{exp_s}").encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(sig, want):
        return None
    return project_id, dst


async def _handle(ws) -> None:
    path = ws.request.path if ws.request else ""
    parsed = urlparse(path)
    if parsed.path.rstrip("/") != "/link":
        await ws.close(code=4404, reason="unknown path")
        return
    token = (parse_qs(parsed.query).get("token") or [""])[0]
    claims = _token_dst(token)
    if claims is None:
        await ws.close(code=4401, reason="bad or expired token")
        return
    project_id, dst = claims

    if dst:
        host, _, port_s = dst.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            await ws.close(code=4400, reason="malformed destination")
            return
    else:
        host, port = "127.0.0.1", settings.mqtt_port

    try:
        reader, writer = await asyncio.open_connection(host, port)
    except OSError as e:
        logger.warning("link bridge: connect to %s failed: %s", dst or "broker", e)
        await ws.close(code=4502, reason="destination unavailable")
        return

    counters = {"up": 0, "down": 0}
    logger.info("link bridge: flow open (project %s → %s)", project_id, dst or "broker")

    async def ws_to_tcp() -> None:
        async for message in ws:
            if isinstance(message, (bytes, bytearray)):
                counters["up"] += len(message)
                writer.write(bytes(message))
                await writer.drain()

    async def tcp_to_ws() -> None:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            counters["down"] += len(data)
            await ws.send(data)

    tasks = [asyncio.create_task(ws_to_tcp()), asyncio.create_task(tcp_to_ws())]
    try:
        # Either side closing (or a long stall) ends the flow.
        done, pending = await asyncio.wait(
            tasks, timeout=None, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
    except Exception:  # noqa: BLE001 — a torn-down splice is normal, not an error
        pass
    finally:
        for t in tasks:
            t.cancel()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
        logger.info(
            "link bridge: flow closed (project %s, %dB up / %dB down)",
            project_id, counters["up"], counters["down"],
        )


async def start_link_bridge() -> None:
    global _server
    _server = await serve(
        _handle, "0.0.0.0", settings.link_port, max_size=2**20, ping_interval=30,
        ping_timeout=_IDLE_TIMEOUT_S,
    )
    logger.info("link bridge listening on :%d (/link)", settings.link_port)


async def stop_link_bridge() -> None:
    global _server
    if _server is not None:
        _server.close()
        await _server.wait_closed()
        _server = None
