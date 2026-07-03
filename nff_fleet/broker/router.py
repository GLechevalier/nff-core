import asyncio
import json
import logging
from typing import Any

from amqtt.client import MQTTClient

from nff_fleet.config import settings
from nff_fleet.registry import registry
from nff_fleet import db
from nff_fleet import ota_result

logger = logging.getLogger(__name__)

# Retained crash payloads keyed by device_id
_crash_cache: dict[str, dict] = {}

# Pending command futures keyed by device_id — one in-flight command per device
_pending: dict[str, asyncio.Future] = {}

# In-flight command to (re)deliver keyed by device_id → (topic, payload_json). A command is
# published QoS 0 with no broker-side queue, so a one-shot publish at send time is lost if the
# device is not connected at that instant. A crash-looping device (the L2 recovery case) is only
# briefly online between reboots, so send_command registers the command here and we REPUBLISH it
# every time we hear from the device (heartbeat/crash on reconnect) until it responds — landing the
# command inside a live connected window. Cleared by send_command's finally.
_redeliver: dict[str, tuple[str, str]] = {}

# Pending rollover-ack futures keyed by hwid — the fulfillment worker awaits the device's
# `rollover_cert` ack on nff/_bootstrap/{batch}/{hwid}/response.
_bootstrap_pending: dict[str, asyncio.Future] = {}

_router_client: MQTTClient | None = None
_router_task: asyncio.Task | None = None


def resolve_response(device_id: str, payload: dict[str, Any]) -> None:
    if fut := _pending.get(device_id):
        if not fut.done():
            fut.set_result(payload)


def register_bootstrap_pending(hwid: str, fut: asyncio.Future) -> None:
    _bootstrap_pending[hwid] = fut


def unregister_bootstrap_pending(hwid: str) -> None:
    _bootstrap_pending.pop(hwid, None)


def resolve_bootstrap_response(hwid: str, payload: dict[str, Any]) -> None:
    if fut := _bootstrap_pending.get(hwid):
        if not fut.done():
            fut.set_result(payload)


def get_crash(device_id: str) -> dict | None:
    return _crash_cache.get(device_id)


def register_pending(device_id: str, fut: asyncio.Future) -> None:
    _pending[device_id] = fut


def unregister_pending(device_id: str) -> None:
    _pending.pop(device_id, None)


def register_redeliver(device_id: str, topic: str, payload: str) -> None:
    _redeliver[device_id] = (topic, payload)


def unregister_redeliver(device_id: str) -> None:
    _redeliver.pop(device_id, None)


async def _maybe_redeliver(device_id: str) -> None:
    """Republish an in-flight command to a device we just heard from (it is connected now).

    Called from the heartbeat/crash handlers: a crash-looping device is only online for a few
    seconds per reboot, so re-sending the command the moment it reconnects is what gets a recovery
    OTA to land. No-op unless a command is registered and its response future is still pending."""
    entry = _redeliver.get(device_id)
    if entry is None:
        return
    fut = _pending.get(device_id)
    if fut is not None and fut.done():
        return
    topic, payload = entry
    try:
        await publish(topic, payload)
        logger.info("Redelivered in-flight command to %s on reconnect", device_id)
    except Exception as exc:  # a redeliver failure must not break message handling
        logger.warning("Redeliver to %s failed: %s", device_id, exc)


async def _handle_message(topic: str, payload_bytes: bytes) -> None:
    try:
        payload = json.loads(payload_bytes.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("Router: unparseable message on %s", topic)
        return

    parts = topic.split("/")

    # nff/_bootstrap/{batch_id}/{hwid}/{type} — claim enrollment channel (shared batch creds).
    if len(parts) >= 5 and parts[0] == "nff" and parts[1] == "_bootstrap":
        batch_id, hwid, msg_type = parts[2], parts[3], parts[4]
        if msg_type == "announce":
            hardware = {
                k: payload[k]
                for k in ("device_type", "fqbn", "chip", "rev", "flash", "fw", "build")
                if payload.get(k) not in (None, "")
            }
            state = await db.record_enrollment(batch_id, hwid, hardware)
            logger.info("Enrollment announce: hwid=%s batch=%s -> %s", hwid, batch_id, state)
        elif msg_type == "response":
            resolve_bootstrap_response(hwid, payload)
        return

    # nff/{project_id}/devices/{id}/{type}
    if len(parts) < 5 or parts[0] != "nff" or parts[2] != "devices":
        return

    project_id = parts[1]
    device_id = parts[3]
    msg_type = parts[4]

    if msg_type == "heartbeat":
        registry.update_heartbeat(device_id, payload, project_id)
        # project_id is authoritative from the cert-scoped topic, not from the device payload.
        payload = {**payload, "project_id": project_id}
        if payload.get("status") == "offline":
            registry.set_offline(device_id)
            db.upsert_device(device_id, {"status": "offline", "project_id": project_id})
        else:
            db.upsert_device(device_id, payload, with_telemetry=True)
            # The device just (re)connected — if a command is waiting for it (e.g. a recovery
            # OTA to a crash-looping device), republish it now while it is live.
            await _maybe_redeliver(device_id)
        logger.debug("Heartbeat from %s (project %s): %s", device_id, project_id,
                     payload.get("status"))

    elif msg_type == "response":
        # An ota_result is published to the same response topic but arrives long after
        # the original command future has resolved/timed out — route it to nff-ota
        # instead of dropping it in resolve_response (which only resolves a pending future).
        if payload.get("type") == "ota_result":
            ota_result.forward_ota_result(device_id, payload)
        else:
            resolve_response(device_id, payload)

    elif msg_type == "crash":
        # project_id is authoritative from the cert-scoped topic, not the device payload
        # (mirror the heartbeat handler above) — so the crash_reports row is tenant-scoped and
        # project-scoped readers (e.g. the agent-worker's senseFleet) actually see it.
        payload = {**payload, "project_id": project_id}
        _crash_cache[device_id] = payload
        db.ingest_crash(device_id, payload)
        logger.info("Crash report cached for %s", device_id)
        # A crash report is published on connect (reporting the prior boot). It is a reliable
        # "device is online right now" signal for a crash-looping device that may report a crash
        # before its heartbeat — republish any in-flight command so the recovery OTA can land.
        await _maybe_redeliver(device_id)


async def _router_loop(client: MQTTClient) -> None:
    broker_url = "mqtt://server:@127.0.0.1:11883/"
    try:
        await client.connect(broker_url, cleansession=True)
        # QoS 0: broker delivers device messages to router without waiting for PUBACK.
        # Wildcard over the project segment: nff/{project_id}/devices/{id}/{type}
        await client.subscribe([("nff/+/devices/#", 0), ("nff/_bootstrap/#", 0)])
        logger.info("Router subscribed to nff/+/devices/# and nff/_bootstrap/#")
        while True:
            message = await client.deliver_message()
            topic = message.publish_packet.variable_header.topic_name
            payload = message.publish_packet.payload.data
            await _handle_message(topic, payload)
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.error("Router loop error: %s", exc)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def start_router() -> None:
    global _router_client, _router_task
    client = MQTTClient(client_id="server")
    _router_client = client
    _router_task = asyncio.create_task(_router_loop(client))
    logger.info("Router started")


async def stop_router() -> None:
    global _router_task
    if _router_task is not None:
        _router_task.cancel()
        try:
            await _router_task
        except asyncio.CancelledError:
            pass
        _router_task = None
    logger.info("Router stopped")


async def publish(topic: str, payload: str, qos: int = 0) -> None:
    if _router_client is None:
        raise RuntimeError("Router not started")
    await _router_client.publish(topic, payload.encode(), qos=qos)
