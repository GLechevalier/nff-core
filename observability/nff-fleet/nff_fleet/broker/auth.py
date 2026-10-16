"""mTLS certificate authentication — binds the connection identity to the verified cert.

DEVICE_OWNERSHIP_DESIGN.md §3: on TLS connect the broker must derive ``(project_id, device_id)``
from the **verified peer certificate**, ignore any self-asserted ``client_id``, and reject a
connection whose ``client_id`` ≠ cert identity. This closes client_id/cert spoofing (T5/T6).

amqtt (0.11.x) hands the auth plugin the ``Session`` and exposes the verified peer cert via
``session.ssl_object.getpeercert(binary_form=True)``. The TLS listener uses ``CERT_OPTIONAL`` with
the fleet CA as ``cafile``, so any cert that reaches us has already been chain-validated; we only
parse it for identity. The internal router connects over the **non-TLS** localhost listener, so
its session has ``ssl_object is None`` — that is the trusted-internal signal, replacing the old
``client_id == "server"`` magic string.

The verified identity is stashed on the session as ``_nff_identity`` for the topic ACL
(``acl.py``) to read without re-parsing the cert on every publish/subscribe.
"""

import logging

from cryptography import x509

from amqtt.plugins.base import BaseAuthPlugin
from amqtt.session import Session

from nff_fleet.provisioning.ca import device_id_from_cert, project_id_from_cert
from nff_fleet.provisioning.batch import (
    BOOTSTRAP_SENTINEL,
    batch_id_from_cert,
    is_batch_cert,
)

logger = logging.getLogger(__name__)

# Sentinel project for the trusted internal (non-TLS, localhost) listener — full access.
INTERNAL = "*"


def session_identity(session: Session) -> tuple[str, str] | None:
    """Return the verified (project_id, device_id) stashed at authenticate time, or None.

    For a bootstrap (shared batch) connection this is ``(BOOTSTRAP_SENTINEL, hwid)``; the batch_id
    is stashed separately on ``session._nff_batch`` for the ACL to scope ``nff/_bootstrap/...``.
    """
    return getattr(session, "_nff_identity", None)


def session_batch(session: Session) -> str | None:
    """Return the batch_id for a bootstrap connection, or None for operational/internal sessions."""
    return getattr(session, "_nff_batch", None)


class NffCertAuthPlugin(BaseAuthPlugin):
    """Authenticate devices from their verified client certificate; allow internal router."""

    async def authenticate(self, *, session: Session) -> bool | None:
        # Internal listener (router): no TLS peer cert. Bound to 127.0.0.1, so trusted.
        if session.ssl_object is None:
            session._nff_identity = (INTERNAL, session.client_id or "")  # type: ignore[attr-defined]
            return True

        der = session.ssl_object.getpeercert(binary_form=True)
        if not der:
            logger.warning("auth deny: TLS client presented no certificate (client_id=%s)",
                           session.client_id)
            return False
        try:
            cert = x509.load_der_x509_certificate(der)
        except Exception as exc:  # malformed cert
            logger.warning("auth deny: unparseable certificate: %s", exc)
            return False

        # Bootstrap (shared batch) cert: every device in the batch presents the SAME cert, so the
        # CN (batch:{id}) can't equal the per-device client_id. Identity is the self-asserted hwid
        # (client_id); the cert only proves "I belong to this batch/project". The ACL confines such
        # a session to nff/_bootstrap/{batch_id}/{hwid}/# until it claims a unique operational cert.
        if is_batch_cert(cert):
            batch_id = batch_id_from_cert(cert)
            hwid = session.client_id
            if not batch_id or not hwid:
                logger.warning("auth deny: bootstrap cert missing batch_id/client_id (batch=%s)",
                               batch_id)
                return False
            session._nff_identity = (BOOTSTRAP_SENTINEL, hwid)  # type: ignore[attr-defined]
            session._nff_batch = batch_id  # type: ignore[attr-defined]
            logger.debug("auth ok (bootstrap): hwid=%s batch=%s", hwid, batch_id)
            return True

        device_id = device_id_from_cert(cert)
        project_id = project_id_from_cert(cert)
        if not device_id or not project_id:
            logger.warning("auth deny: cert missing device_id/project_id (cn=%s project=%s)",
                           device_id, project_id)
            return False
        if session.client_id != device_id:
            logger.warning("auth deny: client_id=%s does not match cert CN=%s",
                           session.client_id, device_id)
            return False

        session._nff_identity = (project_id, device_id)  # type: ignore[attr-defined]
        logger.debug("auth ok: device=%s project=%s", device_id, project_id)
        return True
