"""A minimal anonymous MQTT 3.1.1 broker — the MQTT attack surface (Chain 2).

Implements just the control packets an ``aiomqtt`` client uses at QoS 0/1:
CONNECT→CONNACK(accepted), SUBSCRIBE→SUBACK, PUBLISH (delivered to matching
subscribers, incl. ``#``), PINGREQ→PINGRESP, UNSUBSCRIBE→UNSUBACK, DISCONNECT.

It accepts any (or no) credentials → ``mqtt_probe.anonymous_access = true``, and a
background loop publishes to a control topic (``device/cmd``) so passive
``mqtt_topic_enum`` over ``#`` hears a ``/cmd`` topic, and ``mqtt_publish`` (which
subscribes then publishes) gets its echo back.

Stdlib only — no MQTT library on the server side.
"""

from __future__ import annotations

import asyncio
import logging

from nff_mock_targets.common.banners import Flavor

logger = logging.getLogger(__name__)

# Packet types (high nibble of the fixed header).
_CONNECT, _SUBSCRIBE, _PUBLISH, _UNSUBSCRIBE = 1, 8, 3, 10
_PINGREQ, _DISCONNECT = 12, 14


def _encode_remaining_length(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            break
    return bytes(out)


async def _read_remaining_length(reader: asyncio.StreamReader) -> int:
    multiplier = 1
    value = 0
    while True:
        (b,) = await reader.readexactly(1)
        value += (b & 0x7F) * multiplier
        if not (b & 0x80):
            return value
        multiplier *= 128


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, int, bytes]:
    (b0,) = await reader.readexactly(1)
    length = await _read_remaining_length(reader)
    body = await reader.readexactly(length) if length else b""
    return b0 >> 4, b0 & 0x0F, body


def _encode_publish(topic: str, payload: bytes) -> bytes:
    tb = topic.encode()
    body = len(tb).to_bytes(2, "big") + tb + payload  # QoS 0: no packet id
    return bytes([_PUBLISH << 4]) + _encode_remaining_length(len(body)) + body


def _topic_matches(filt: str, topic: str) -> bool:
    fs = filt.split("/")
    ts = topic.split("/")
    for i, fp in enumerate(fs):
        if fp == "#":
            return True
        if i >= len(ts):
            return False
        if fp == "+":
            continue
        if fp != ts[i]:
            return False
    return len(fs) == len(ts)


class _Conn:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.filters: set[str] = set()

    def matches(self, topic: str) -> bool:
        return any(_topic_matches(f, topic) for f in self.filters)


class MqttBroker:
    def __init__(
        self, flavor: Flavor, bind: str, port: int, control_topic: str, publish_interval_s: float
    ) -> None:
        self.flavor = flavor
        self.bind = bind
        self.port = port
        self.control_topic = control_topic
        self.publish_interval_s = publish_interval_s
        self._conns: list[_Conn] = []

    async def _deliver(self, topic: str, payload: bytes) -> None:
        pkt = _encode_publish(topic, payload)
        for conn in list(self._conns):
            if conn.matches(topic):
                try:
                    conn.writer.write(pkt)
                    await conn.writer.drain()
                except (ConnectionError, RuntimeError):
                    pass

    async def _control_loop(self) -> None:
        seq = 0
        while True:
            await asyncio.sleep(self.publish_interval_s)
            seq += 1
            await self._deliver(self.control_topic, f'{{"seq":{seq},"action":"noop"}}'.encode())

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = _Conn(writer)
        peer = writer.get_extra_info("peername")
        try:
            while True:
                ptype, flags, body = await _read_packet(reader)

                if ptype == _CONNECT:
                    # Accept unconditionally (anonymous): CONNACK, session=0, rc=0.
                    writer.write(b"\x20\x02\x00\x00")
                    await writer.drain()
                    self._conns.append(conn)
                    logger.info("mqtt CONNECT accepted (anonymous) from %s", peer)

                elif ptype == _SUBSCRIBE:
                    pid = body[:2]
                    filters = _parse_subscribe(body[2:])
                    conn.filters.update(filters)
                    suback = b"\x90" + _encode_remaining_length(2 + len(filters)) + pid + bytes(len(filters))
                    writer.write(suback)
                    await writer.drain()
                    logger.info("mqtt SUBSCRIBE %s from %s", filters, peer)

                elif ptype == _PUBLISH:
                    qos = (flags >> 1) & 0x03
                    topic, payload, pid = _parse_publish(body, qos)
                    if qos == 1 and pid:
                        writer.write(b"\x40\x02" + pid)  # PUBACK
                        await writer.drain()
                    await self._deliver(topic, payload)

                elif ptype == _UNSUBSCRIBE:
                    pid = body[:2]
                    for f in _parse_unsubscribe(body[2:]):
                        conn.filters.discard(f)
                    writer.write(b"\xb0\x02" + pid)  # UNSUBACK
                    await writer.drain()

                elif ptype == _PINGREQ:
                    writer.write(b"\xd0\x00")  # PINGRESP
                    await writer.drain()

                elif ptype == _DISCONNECT:
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if conn in self._conns:
                self._conns.remove(conn)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def serve(self) -> None:
        server = await asyncio.start_server(self._handle, self.bind, self.port)
        addr = ", ".join(str(s.getsockname()) for s in server.sockets)
        logger.info("mqtt broker listening on %s (control topic %s)", addr, self.control_topic)
        ctrl = asyncio.create_task(self._control_loop())
        try:
            async with server:
                await server.serve_forever()
        finally:
            ctrl.cancel()
            await asyncio.gather(ctrl, return_exceptions=True)


def _parse_subscribe(payload: bytes) -> list[str]:
    filters: list[str] = []
    i = 0
    while i + 2 <= len(payload):
        ln = int.from_bytes(payload[i : i + 2], "big")
        i += 2
        filters.append(payload[i : i + ln].decode(errors="replace"))
        i += ln + 1  # skip the 1-byte requested QoS
    return filters


def _parse_unsubscribe(payload: bytes) -> list[str]:
    filters: list[str] = []
    i = 0
    while i + 2 <= len(payload):
        ln = int.from_bytes(payload[i : i + 2], "big")
        i += 2
        filters.append(payload[i : i + ln].decode(errors="replace"))
        i += ln
    return filters


def _parse_publish(body: bytes, qos: int) -> tuple[str, bytes, bytes]:
    ln = int.from_bytes(body[0:2], "big")
    topic = body[2 : 2 + ln].decode(errors="replace")
    j = 2 + ln
    pid = b""
    if qos > 0:
        pid = body[j : j + 2]
        j += 2
    return topic, body[j:], pid
