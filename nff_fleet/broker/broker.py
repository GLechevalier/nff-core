import asyncio
import logging
from pathlib import Path

from amqtt.broker import Broker
from amqtt.session import Session

from nff_fleet.broker.acl import topic_allowed
from nff_fleet.config import settings

logger = logging.getLogger(__name__)

_broker_instance: Broker | None = None


class NffBroker(Broker):
    """amqtt Broker with retained-message replay scoped to the connecting client's ACL.

    Stock amqtt 0.11.3 replays retained messages for **every** global subscription filter to
    **every** connecting client (``_handle_client_session`` iterates ``self._subscriptions``) and
    ``_publish_retained_messages_for_subscription`` does so with **no ACL check**. Because the
    internal router subscribes to ``nff/+/devices/#`` + ``nff/_bootstrap/#``, that means any device
    that connects is handed the whole fleet's retained heartbeats and announces — a flood that
    starves the device's keepalive (it disconnects/reconnects, re-flooding) **and** a tenant-
    isolation leak (a device sees other devices', even other tenants', retained state — violates
    DEVICE_OWNERSHIP_DESIGN.md §7). We override the replay to deliver only retained topics the
    target session is authorised to receive (its own project+device namespace; the router keeps
    full access). Couples to amqtt 0.11.3 internals (``_retained_messages``/``_matches``/
    ``_get_handler``) by necessity — revisit on an amqtt upgrade.
    """

    async def _publish_retained_messages_for_subscription(
        self, subscription: tuple[str, int], session: Session
    ) -> None:
        topic_filter, qos = subscription
        handler = self._get_handler(session)
        if handler is None:
            return
        tasks = [
            asyncio.ensure_future(
                handler.mqtt_publish(
                    retained.topic, retained.data, min(qos, retained.qos or qos), retain=True
                )
            )
            for topic, retained in list(self._retained_messages.items())
            if self._matches(topic, topic_filter) and topic_allowed(session, topic, quiet=True)
        ]
        if tasks:
            await asyncio.wait(tasks)


def _build_config(
    ca_cert: Path,
    server_cert: Path,
    server_key: Path,
    port: int,
) -> dict:
    return {
        "listeners": {
            "default": {
                "type": "tcp",
                "bind": "127.0.0.1:11883",
                "ssl": False,
            },
            "tls": {
                "type": "tcp",
                "bind": f"0.0.0.0:{port}",
                "ssl": True,
                "cafile": str(ca_cert),
                "certfile": str(server_cert),
                "keyfile": str(server_key),
            },
        },
        "sys_interval": 10,
        # Plugins are loaded directly from this dict (amqtt 0.11.x). The cert-auth plugin is the
        # sole authentication authority — there is NO anonymous fallback: a TLS client is
        # identified by its verified cert, and the non-TLS internal listener is trusted by the
        # same plugin (ssl_object is None). The topic ACL scopes each client to its own
        # project+device namespace. When plugins come from this dict, the legacy "auth" /
        # "topic-check" config sections are intentionally omitted (amqtt ignores them).
        "plugins": {
            "nff_fleet.broker.auth.NffCertAuthPlugin": {},
            "nff_fleet.broker.acl.NffTopicAclPlugin": {},
        },
    }


async def start_broker() -> Broker:
    global _broker_instance
    config = _build_config(
        ca_cert=settings.tls_ca_cert,
        server_cert=settings.tls_server_cert,
        server_key=settings.tls_server_key,
        port=settings.mqtt_port,
    )
    broker = NffBroker(config)
    await broker.start()
    _broker_instance = broker
    logger.info("MQTT broker started on port %d", settings.mqtt_port)
    return broker


async def stop_broker() -> None:
    global _broker_instance
    if _broker_instance is not None:
        await _broker_instance.shutdown()
        _broker_instance = None
        logger.info("MQTT broker stopped")
