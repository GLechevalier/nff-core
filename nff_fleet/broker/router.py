import asyncio
import json
import logging
from typing import Any

from amqtt.client import MQTTClient

from nff_fleet.config import settings
from nff_fleet.registry import registry

logger = logging.getLogger(__name__)

# Retained crash payloads keyed by device_id
_crash_cache: dict[str, dict] = {}

# Pending command futures keyed by device_id — one in-flight command per device
_pending: dict[str, asyncio.Future] = {}

_router_client: MQTTClient | None = None
_router_task: asyncio.Task | None = None


def resolve_response(device_id: str, payload: dict[str, Any]) -> None:
    if fut := _pending.get(device_id):
        if not fut.done():
            fut.set_result(payload)


def get_crash(device_id: str) -> dict | None:
    return _crash_cache.get(device_id)


def register_pending(device_id: str, fut: asyncio.Future) -> None:
    _pending[device_id] = fut


def unregister_pending(device_id: str) -> None:
    _pending.pop(device_id, None)


async def _handle_message(topic: str, payload_bytes: bytes) -> None:
    try:
        payload = json.loads(payload_bytes.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("Router: unparseable message on %s", topic)
        return

    parts = topic.split("/")
    # nff/devices/{id}/{type}
    if len(parts) < 4 or parts[0] != "nff" or parts[1] != "devices":
        return

    device_id = parts[2]
    msg_type = parts[3]

    if msg_type == "heartbeat":
        registry.update_heartbeat(device_id, payload)
        if payload.get("status") == "offline":
            registry.set_offline(device_id)
        logger.debug("Heartbeat from %s: %s", device_id, payload.get("status"))

    elif msg_type == "response":
        resolve_response(device_id, payload)

    elif msg_type == "crash":
        _crash_cache[device_id] = payload
        logger.info("Crash report cached for %s", device_id)


async def _router_loop(client: MQTTClient) -> None:
    broker_url = "mqtt://server:@127.0.0.1:11883/"
    try:
        await client.connect(broker_url, cleansession=True)
        # QoS 0: broker delivers device messages to router without waiting for PUBACK
        await client.subscribe([("nff/devices/#", 0)])
        logger.info("Router subscribed to nff/devices/#")
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
