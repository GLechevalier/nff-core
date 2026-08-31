"""Batch bootstrap credentials — one shared cert per flashed batch (DEVICE_OWNERSHIP_DESIGN.md §8).

A customer flashes a whole batch of devices with ONE shared bootstrap credential (one firmware
image, one OTA). The shared cert is signed by the project's intermediate CA, so it chains to the
root and is cryptographically pinned to the project, but it carries NO per-device identity: every
device in the batch presents the same cert, sets its own hardware id as the MQTT client_id, and is
allowed by the broker ONLY on the bootstrap namespace ``nff/_bootstrap/{batch_id}/{hwid}/#`` until
it is accepted and rolled over to a unique per-device operational cert.

The cert is marked with ``NFF_BATCH_OID`` (presence ⇒ bootstrap/batch cert) and carries the
``batch_id`` as that extension's value; ``project_id`` rides in ``NFF_PROJECT_OID`` like an
operational leaf.
"""

import datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from nff_fleet.config import settings
from nff_fleet.provisioning.ca import NFF_PROJECT_OID, load_project_ca

# Custom OID whose presence marks a cert as a shared batch/bootstrap credential; its value is the
# batch_id. Under the same private enterprise arc as NFF_PROJECT_OID (…1.1).
NFF_BATCH_OID = x509.ObjectIdentifier("1.3.6.1.4.1.61226.1.2")

# Sentinel used in the broker session identity for a bootstrap connection — never a real project_id
# (which is always a uuid), so the operational ACL path can never be reached by a bootstrap device.
BOOTSTRAP_SENTINEL = "_bootstrap"


def _batch_dir(batch_id: str) -> Path:
    return settings.certs_dir / "batches" / batch_id


def is_batch_cert(cert: x509.Certificate) -> bool:
    """True iff the cert carries the NFF_BATCH_OID marker (a shared bootstrap credential)."""
    try:
        cert.extensions.get_extension_for_oid(NFF_BATCH_OID)
        return True
    except x509.ExtensionNotFound:
        return False


def batch_id_from_cert(cert: x509.Certificate) -> str | None:
    """Return the batch_id from the signed NFF_BATCH_OID extension, or None."""
    try:
        ext = cert.extensions.get_extension_for_oid(NFF_BATCH_OID)
    except x509.ExtensionNotFound:
        return None
    value = ext.value
    raw = value.value if isinstance(value, x509.UnrecognizedExtension) else value
    try:
        return raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
    except UnicodeDecodeError:
        return None


def issue_batch_cert(project_id: str, batch_id: str) -> tuple[Path, Path]:
    """Issue the shared bootstrap leaf for a batch, signed by the project's intermediate CA.

    CN = ``batch:{batch_id}`` (shared across the batch); carries NFF_PROJECT_OID + NFF_BATCH_OID.
    Stored at ``{certs_dir}/batches/{batch_id}/bootstrap.{crt,key}``. Returns (cert_path, key_path).
    """
    ca_key, ca_cert = load_project_ca(project_id)
    out_dir = _batch_dir(batch_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"batch:{batch_id}")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.UnrecognizedExtension(NFF_PROJECT_OID, project_id.encode()), critical=False)
        .add_extension(x509.UnrecognizedExtension(NFF_BATCH_OID, batch_id.encode()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    key_path = out_dir / "bootstrap.key"
    cert_path = out_dir / "bootstrap.crt"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def batch_cert_serial(batch_id: str) -> str | None:
    """Hex serial of the batch's bootstrap cert (for the DB batch record / audit), or None."""
    cert_path = _batch_dir(batch_id) / "bootstrap.crt"
    if not cert_path.exists():
        return None
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return format(cert.serial_number, "x")
