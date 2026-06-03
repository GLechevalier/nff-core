import asyncio
import logging
from pathlib import Path

from amqtt.broker import Broker

from nff_fleet.config import settings

logger = logging.getLogger(__name__)

_broker_instance: Broker | None = None


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
        "auth": {
            "plugins": ["auth_anonymous"],
            "allow-anonymous": False,
        },
        "plugins": [
            "amqtt.plugins.authentication.AnonymousAuthPlugin",
            "nff_fleet.broker.acl.NffAclPlugin",
        ],
        "auth": {"allow-anonymous": True},
        "topic-check": {
            "enabled": True,
            "plugins": ["nff_fleet.broker.acl.NffAclPlugin"],
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
    broker = Broker(config)
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
