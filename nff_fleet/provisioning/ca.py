import datetime
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from nff_fleet.config import settings

_CA_KEY_PATH = lambda: settings.certs_dir / "ca.key"
_CA_CERT_PATH = lambda: settings.certs_dir / "ca.crt"

# Custom X.509 extension OID carrying the device's project_id (tenant scope), signed
# by the CA and therefore tamper-proof (DEVICE_OWNERSHIP_DESIGN.md §5 Option B). Under a
# private enterprise arc (1.3.6.1.4.1.<PEN>.1.1) — replace the placeholder PEN 61226 with a
# registered IANA enterprise number before any external/long-lived deployment.
NFF_PROJECT_OID = x509.ObjectIdentifier("1.3.6.1.4.1.61226.1.1")

_ca_key: ec.EllipticCurvePrivateKey | None = None
_ca_cert: x509.Certificate | None = None

# Per-project intermediate CAs (DEVICE_OWNERSHIP_DESIGN.md §5 Option A1): the root signs a
# per-project intermediate, device leaf certs chain to their project's intermediate, and the
# broker (cafile = root) validates the chain a device presents (leaf + intermediate). This gives
# per-tenant revocation / blast-radius containment on a single shared TLS listener. The project is
# still read from the signed NFF_PROJECT_OID leaf extension, so no chain parsing happens on connect.
_project_cas: dict[str, tuple[ec.EllipticCurvePrivateKey, x509.Certificate]] = {}


def device_id_from_cert(cert: x509.Certificate) -> str | None:
    """Return the device_id (cert CN), or None if absent."""
    try:
        cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    except x509.ExtensionNotFound:
        return None
    return cn[0].value if cn else None


def project_id_from_cert(cert: x509.Certificate) -> str | None:
    """Return the project_id from the signed NFF_PROJECT_OID extension, or None."""
    try:
        ext = cert.extensions.get_extension_for_oid(NFF_PROJECT_OID)
    except x509.ExtensionNotFound:
        return None
    value = ext.value
    raw = value.value if isinstance(value, x509.UnrecognizedExtension) else value
    try:
        return raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
    except UnicodeDecodeError:
        return None


def _load_ca() -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    global _ca_key, _ca_cert
    if _ca_key is None:
        key_pem = _CA_KEY_PATH().read_bytes()
        cert_pem = _CA_CERT_PATH().read_bytes()
        _ca_key = serialization.load_pem_private_key(key_pem, password=None)
        _ca_cert = x509.load_pem_x509_certificate(cert_pem)
    return _ca_key, _ca_cert


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _project_ca_paths(project_id: str) -> tuple[Path, Path]:
    base = settings.project_keys_dir / project_id
    return base / "ca.key", base / "ca.crt"


def ensure_project_ca(project_id: str) -> tuple[Path, Path]:
    """Create the project's intermediate CA (signed by the root) if absent. Idempotent.

    Returns (cert_path, key_path). The intermediate has ``BasicConstraints(ca=True, path_length=0)``
    so it may sign leaves but no further CAs.
    """
    key_path, cert_path = _project_ca_paths(project_id)
    if key_path.exists() and cert_path.exists():
        return cert_path, key_path

    root_key, root_cert = _load_ca()
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"nff-project-ca:{project_id}")])
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(root_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(root_cert.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(root_key, hashes.SHA256())
    )
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def load_project_ca(project_id: str) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    """Load (and cache) the project's intermediate CA keypair, creating it if needed."""
    cached = _project_cas.get(project_id)
    if cached is not None:
        return cached
    cert_path, key_path = ensure_project_ca(project_id)
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    pair = (key, cert)
    _project_cas[project_id] = pair
    return pair


def project_ca_cert_der(project_id: str) -> bytes:
    """DER of the project's intermediate CA cert (embedded in credentials.h as the chain link)."""
    _, cert = load_project_ca(project_id)
    return cert.public_bytes(serialization.Encoding.DER)


def generate_ca() -> None:
    """Generate a P-256 CA keypair and self-signed certificate with required extensions."""
    settings.certs_dir.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "nff-fleet-ca")])
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    _CA_KEY_PATH().write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    _CA_CERT_PATH().write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def issue_server_cert(sans: str | list[str]) -> tuple[Path, Path]:
    """Issue a P-256 server cert signed by the **root** CA, valid for every address in ``sans``.

    ``sans`` is one address or a list of them; each entry is classified as an IP literal or a
    DNS name automatically, so a single cert can cover several broker addresses at once
    (e.g. a laptop reachable at two LAN IPs, or a VPS reachable by domain). Loopback
    (``127.0.0.1`` / ``localhost``) is always included for local tooling.

    Re-mints ONLY ``server.crt`` / ``server.key`` — the root CA, the per-project intermediate
    CAs, and all device leaf certs are untouched, so devices that pin the root keep working.
    Use this (not ``generate_ca``/``init``) when the broker's address changes.
    """
    if isinstance(sans, str):
        sans = [sans]
    ca_key, ca_cert = _load_ca()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "nff-fleet-server")])
    now = _now()
    alt = []
    for entry in [*sans, "127.0.0.1", "localhost"]:
        if any(getattr(g, "value", None) == entry or str(getattr(g, "value", "")) == entry for g in alt):
            continue  # de-dup repeated addresses
        try:
            ip = ipaddress.ip_address(entry)
            alt.append(x509.IPAddress(ip))
            # Also emit the IP literal as a dNSName. mbedtls (the ESP32 / L2-QEMU firmware) matches
            # the verify-hostname STRING against dNSName SANs only — it does NOT match an IP literal
            # against iPAddress SANs — so an L2 device connecting to a bare broker IP (guest_broker_ip
            # = 10.13.0.10) fails X.509 verification (-9984) unless the IP is also present as a DNS
            # name. OpenSSL/Python clients still match the iPAddress form. Belt-and-suspenders.
            alt.append(x509.DNSName(entry))
        except ValueError:
            alt.append(x509.DNSName(entry))
    san = x509.SubjectAlternativeName(alt)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(ca_cert.subject)
        .public_key((key := ec.generate_private_key(ec.SECP256R1())).public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(san, critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    key_path = settings.certs_dir / "server.key"
    cert_path = settings.certs_dir / "server.crt"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def issue_device_cert(device_id: str, project_id: str) -> tuple[Path, Path]:
    """Issue a P-256 client cert signed by the CA, binding device_id (CN) to project_id.

    The project_id is embedded in the signed NFF_PROJECT_OID extension so the broker can
    derive the tenant scope from the verified certificate alone — never from a self-asserted
    client_id or payload (DEVICE_OWNERSHIP_DESIGN.md §3, §5 Option B). The leaf is signed by the
    project's **intermediate CA** (§5 A1), so it chains leaf → intermediate → root.
    """
    ca_key, ca_cert = load_project_ca(project_id)
    device_dir = settings.certs_dir / device_id
    device_dir.mkdir(parents=True, exist_ok=True)

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, device_id)])
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.UnrecognizedExtension(NFF_PROJECT_OID, project_id.encode()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    key_path = device_dir / "device.key"
    cert_path = device_dir / "device.crt"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def clear_cache() -> None:
    """Drop cached CA material (used by tests that swap settings.data_dir)."""
    global _ca_key, _ca_cert
    _ca_key = None
    _ca_cert = None
    _project_cas.clear()
