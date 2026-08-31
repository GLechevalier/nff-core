"""Fleet reliability hardening: the router never dies silently, and a claim self-heals even when
the rollover or its ack is lost. No real broker/DB — the internal client and db wrappers are faked.
"""
import asyncio
import types
from collections import deque

import pytest

from nff_fleet.broker import router
from nff_fleet import enroll_worker


def _msg(topic: str, payload: bytes):
    """A stand-in for amqtt's delivered message (only the fields _router_loop reads)."""
    return types.SimpleNamespace(
        publish_packet=types.SimpleNamespace(
            variable_header=types.SimpleNamespace(topic_name=topic),
            payload=types.SimpleNamespace(data=payload),
        )
    )


class _FakeClient:
    """Scriptable amqtt client. `script` is a list of ('msg', topic, payload) | ('raise', exc);
    deliver_message replays it, then raises CancelledError to end the loop."""

    def __init__(self, script):
        self.script = list(script)
        self.connects = 0
        self.subscribes = 0
        self.disconnects = 0

    async def connect(self, url, cleansession=True):
        self.connects += 1

    async def subscribe(self, subs):
        self.subscribes += 1

    async def deliver_message(self):
        if not self.script:
            raise asyncio.CancelledError()
        kind, *rest = self.script.pop(0)
        if kind == "msg":
            return _msg(rest[0], rest[1])
        raise rest[0]

    async def disconnect(self):
        self.disconnects += 1


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    async def _nosleep(_):
        return None
    monkeypatch.setattr(router.asyncio, "sleep", _nosleep)
    router.registry._devices.clear()


def test_router_reconnects_after_connection_error(monkeypatch):
    # Client A drops mid-deliver; the loop must reconnect with a fresh client (B) and keep going.
    a = _FakeClient([("raise", ConnectionResetError("drop"))])
    b = _FakeClient([("msg", "nff/p/devices/d/heartbeat", b'{"status":"online"}'),
                     ("raise", asyncio.CancelledError())])
    clients = deque([a, b])
    monkeypatch.setattr(router, "MQTTClient", lambda **kw: clients.popleft())

    handled = []
    async def fake_handle(topic, payload):
        handled.append(topic)
    monkeypatch.setattr(router, "_handle_message", fake_handle)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(router._router_loop())

    assert a.connects == 1 and b.connects == 1  # reconnected, did not die on the drop
    assert b.subscribes == 1                     # and re-subscribed
    assert handled == ["nff/p/devices/d/heartbeat"]


def test_router_survives_handler_error(monkeypatch):
    # A handler exception on one message must not kill the loop — the next message is still handled.
    c = _FakeClient([("msg", "nff/p/devices/d/crash", b"{}"),
                     ("msg", "nff/p/devices/d/heartbeat", b'{"status":"online"}'),
                     ("raise", asyncio.CancelledError())])
    monkeypatch.setattr(router, "MQTTClient", lambda **kw: c)

    seen = []
    async def flaky_handle(topic, payload):
        seen.append(topic)
        if len(seen) == 1:
            raise RuntimeError("boom")
    monkeypatch.setattr(router, "_handle_message", flaky_handle)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(router._router_loop())

    assert seen == ["nff/p/devices/d/crash", "nff/p/devices/d/heartbeat"]  # survived the first error


def test_heartbeat_finalizes_claim_by_presence(monkeypatch):
    # An operational heartbeat finalizes an accepted enrollment (lost-ack recovery), once, on connect.
    called = []
    async def fake_claim(device_id):
        called.append(device_id)
        return 1
    monkeypatch.setattr(router.db, "claim_enrollment_by_device", fake_claim)

    hb = "nff/proj-1/devices/dev-1/heartbeat"
    asyncio.run(router._handle_message(hb, b'{"status":"online"}'))
    assert called == ["dev-1"]                          # finalized on first (connect) beat

    asyncio.run(router._handle_message(hb, b'{"status":"online"}'))
    assert called == ["dev-1"]                          # gated: not re-run while already online

    called.clear()
    asyncio.run(router._handle_message(
        "nff/proj-1/devices/dev-2/heartbeat", b'{"status":"offline"}'))
    assert called == []                                 # an offline (LWT) beat never claims


def test_worker_loop_runs_reconcile_each_sweep(monkeypatch):
    calls = {"reconcile": 0, "expire": 0, "pending": 0}

    async def fake_reconcile(grace):
        calls["reconcile"] += 1
        return 0
    async def fake_expire():
        calls["expire"] += 1
        return []
    async def fake_pending(n):
        calls["pending"] += 1
        return []
    monkeypatch.setattr(enroll_worker.db, "reconcile_stale_enrollments", fake_reconcile)
    monkeypatch.setattr(enroll_worker.db, "expire_stale_batches", fake_expire)
    monkeypatch.setattr(enroll_worker.db, "get_accepted_unfulfilled", fake_pending)

    # Let exactly one sweep run: sleep returns the first time, ends the loop the second.
    n = {"i": 0}
    async def one_sweep(_):
        n["i"] += 1
        if n["i"] >= 2:
            raise asyncio.CancelledError()
    monkeypatch.setattr(enroll_worker.asyncio, "sleep", one_sweep)

    asyncio.run(enroll_worker._worker_loop())  # _worker_loop swallows CancelledError
    assert calls == {"reconcile": 1, "expire": 1, "pending": 1}
