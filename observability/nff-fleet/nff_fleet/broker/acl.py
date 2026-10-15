"""Topic ACL — scopes every device to its own project + device namespace.

DEVICE_OWNERSHIP_DESIGN.md §7: topics are namespaced ``nff/{project_id}/devices/{device_id}/#``
and the ACL derives ``(project_id, device_id)`` from the **verified certificate** (stashed on the
session by ``auth.py``), never from a self-asserted ``client_id``. There is no ``"server"`` magic
string: the internal router authenticates over the non-TLS localhost listener and carries the
``INTERNAL`` sentinel, which grants full access.

amqtt (0.11.x) calls ``topic_filtering`` for SUBSCRIBE, PUBLISH and RECEIVE; the broker denies
unless **all** topic plugins return truthy (a ``None`` return denies), so this returns explicit
booleans.
"""

import logging

from amqtt.plugins.base import BaseTopicPlugin
from amqtt.session import Session

from nff_fleet.broker.auth import INTERNAL, session_batch, session_identity
from nff_fleet.provisioning.batch import BOOTSTRAP_SENTINEL

logger = logging.getLogger(__name__)


def _topic_scope(topic: str) -> tuple[str, str] | None:
    """Return (project_id, device_id) for a nff/{project}/devices/{device}/... topic, else None."""
    parts = topic.split("/")
    if len(parts) < 4 or parts[0] != "nff" or parts[2] != "devices":
        return None
    project_id, device_id = parts[1], parts[3]
    if not project_id or not device_id:
        return None
    return project_id, device_id


def topic_allowed(session: Session, topic: str, *, quiet: bool = False) -> bool:
    """True if `session`'s verified identity may use `topic` (its own project+device namespace).

    Shared by the SUBSCRIBE/PUBLISH ACL plugin and the broker's retained-replay override (which
    amqtt 0.11.3 performs with no ACL check — see broker.NffBroker). `quiet` suppresses the
    deny-logging for the high-volume retained-replay path.
    """
    identity = session_identity(session)
    if identity is None:
        if not quiet:
            logger.warning("ACL deny: no verified identity for client=%s topic=%s",
                           getattr(session, "client_id", "?"), topic)
        return False

    project_id, device_id = identity
    if project_id == INTERNAL:
        return True  # internal router: full access to nff/+/devices/#

    if project_id == BOOTSTRAP_SENTINEL:
        # Bootstrap (shared batch) device: confined to its own announce/cmd/response under
        # nff/_bootstrap/{batch_id}/{hwid}/# — never any project namespace.
        batch_id = session_batch(session)
        parts = topic.split("/")
        allowed = (
            batch_id is not None
            and len(parts) >= 4
            and parts[0] == "nff"
            and parts[1] == "_bootstrap"
            and parts[2] == batch_id
            and parts[3] == device_id   # device_id slot holds the hwid for bootstrap
        )
        if not allowed and not quiet:
            logger.warning("ACL deny: bootstrap hwid=%s batch=%s tried topic=%s",
                           device_id, batch_id, topic)
        return allowed

    scope = _topic_scope(topic)
    if scope is None:
        if not quiet:
            logger.warning("ACL deny: client=%s topic=%s not in nff/{project}/devices/ form",
                           device_id, topic)
        return False
    t_project, t_device = scope
    allowed = t_project == project_id and t_device == device_id
    if not allowed and not quiet:
        logger.warning(
            "ACL deny: device=%s project=%s tried topic=%s (owner project=%s device=%s)",
            device_id, project_id, topic, t_project, t_device,
        )
    return allowed


class NffTopicAclPlugin(BaseTopicPlugin):
    """Allow a client only within its own project's device namespace."""

    async def topic_filtering(self, *, session: Session | None = None,
                              topic: str | None = None, action=None) -> bool:
        if session is None or topic is None:
            return False
        return topic_allowed(session, topic)
