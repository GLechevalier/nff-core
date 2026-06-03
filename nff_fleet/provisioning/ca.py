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

_ca_key: ec.EllipticCurvePrivateKey | None = None
_ca_cert: x509.Certificate | None = None


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


def issue_server_cert(broker_ip: str) -> tuple[Path, Path]:
    """Issue a P-256 server cert signed by the CA, with SAN for broker_ip and localhost."""
    ca_key, ca_cert = _load_ca()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "nff-fleet-server")])
    now = _now()
    san = x509.SubjectAlternativeName([
        x509.IPAddress(ipaddress.ip_address(broker_ip)),
        x509.DNSName("localhost"),
    ])
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


def issue_device_cert(device_id: str) -> tuple[Path, Path]:
    """Issue a P-256 client cert signed by the CA for the given device_id."""
    ca_key, ca_cert = _load_ca()
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
