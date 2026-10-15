"""Reconnect-redelivery of an in-flight command (the L2 crash-loop recovery path).

A QoS-0 command publish is dropped if the device is offline at send time, and a crash-looping
device is only briefly online between reboots — so a one-shot publish never lands. The router
republishes a registered command whenever the device is heard from (heartbeat/crash) until its
response future resolves. nff-fleet has no pytest-asyncio, so handlers run via asyncio.run().
"""
import asyncio
import json

from nff_fleet.broker import router


def _neuter(monkeypatch, published):
    async def fake_publish(topic, payload, qos=0):
        published.append((topic, payload))

    monkeypatch.setattr(router, "publish", fake_publish)
    monkeypatch.setattr(router.registry, "update_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(router.registry, "set_offline", lambda *a, **k: None)
    monkeypatch.setattr(router.db, "upsert_device", lambda *a, **k: None)
    monkeypatch.setattr(router.db, "ingest_crash", lambda *a, **k: None)


def test_redeliver_on_heartbeat_until_resolved(monkeypatch):
    published: list = []
    _neuter(monkeypatch, published)
    dev, topic, body = "dev-1", "nff/p1/devices/dev-1/cmd", '{"action":"ota"}'

    async def scenario():
        fut = asyncio.get_event_loop().create_future()
        router.register_pending(dev, fut)
        router.register_redeliver(dev, topic, body)
        hb = f"nff/p1/devices/{dev}/heartbeat"

        # Device reconnects (online heartbeat) → command is republished in the live window.
        await router._handle_message(hb, json.dumps({"status": "online"}).encode())
        assert (topic, body) in published

        # A crash report is also a "device is online now" signal → also republishes.
        published.clear()
        await router._handle_message(
            f"nff/p1/devices/{dev}/crash", json.dumps({"pc": 1}).encode()
        )
        assert (topic, body) in published

        # Once the response future resolves, no more redelivery.
        published.clear()
        fut.set_result({"ok": True})
        await router._handle_message(hb, json.dumps({"status": "online"}).encode())
        assert not published

        router.unregister_pending(dev)
        router.unregister_redeliver(dev)

    asyncio.run(scenario())


def test_no_redeliver_on_offline_or_without_registration(monkeypatch):
    published: list = []
    _neuter(monkeypatch, published)
    dev, topic, body = "dev-2", "nff/p1/devices/dev-2/cmd", '{"action":"ota"}'

    async def scenario():
        hb = f"nff/p1/devices/{dev}/heartbeat"
        # No command registered → a heartbeat republishes nothing.
        await router._handle_message(hb, json.dumps({"status": "online"}).encode())
        assert not published

        # Registered, but an OFFLINE heartbeat (LWT) must NOT republish (device is gone).
        fut = asyncio.get_event_loop().create_future()
        router.register_pending(dev, fut)
        router.register_redeliver(dev, topic, body)
        await router._handle_message(hb, json.dumps({"status": "offline"}).encode())
        assert not published
        router.unregister_pending(dev)
        router.unregister_redeliver(dev)

    asyncio.run(scenario())
