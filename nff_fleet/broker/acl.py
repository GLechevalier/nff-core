import logging
from amqtt.plugins.manager import BasePlugin

logger = logging.getLogger(__name__)

_NFF_PREFIX = "nff/devices/"


def _device_id_from_topic(topic: str) -> str | None:
    """Return the device_id segment from a nff/devices/{id}/... topic."""
    if not topic.startswith(_NFF_PREFIX):
        return None
    rest = topic[len(_NFF_PREFIX):]
    parts = rest.split("/", 1)
    return parts[0] if parts[0] else None


class NffAclPlugin(BasePlugin):
    """
    amqtt ACL plugin.

    Each connecting device authenticates with a client certificate whose CN is the
    device_id.  This plugin enforces that a device can only publish/subscribe to its
    own nff/devices/{device_id}/# topics.

    The internal router client authenticates with username "server" and is allowed
    full access to nff/devices/#.
    """

    async def on_broker_client_connected(self, *args, **kwargs) -> None:
        client_id = kwargs.get("client_id") or (args[0] if args else "?")
        logger.debug("ACL: client connected client_id=%s", client_id)

    async def authorize_topic_subscribe(self, client_id: str, topic: str) -> bool:
        if client_id == "server":
            return True
        device_id = _device_id_from_topic(topic)
        if device_id is None:
            logger.warning("ACL deny subscribe: client=%s topic=%s", client_id, topic)
            return False
        allowed = client_id == device_id
        if not allowed:
            logger.warning(
                "ACL deny subscribe: client=%s tried topic=%s (owner=%s)",
                client_id,
                topic,
                device_id,
            )
        return allowed

    async def authorize_topic_publish(self, client_id: str, topic: str) -> bool:
        if client_id == "server":
            return True
        device_id = _device_id_from_topic(topic)
        if device_id is None:
            logger.warning("ACL deny publish: client=%s topic=%s", client_id, topic)
            return False
        allowed = client_id == device_id
        if not allowed:
            logger.warning(
                "ACL deny publish: client=%s tried topic=%s (owner=%s)",
                client_id,
                topic,
                device_id,
            )
        return allowed
