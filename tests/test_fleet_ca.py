"""Tests for POST /internal/fleet-ca (build-time CA export for nff-github).

nff-fleet has no pytest-asyncio config, so async handlers are driven via asyncio.run().
"""
import asyncio
import base64
import datetime
import json

from nff_fleet import control
from nff_fleet.config import settings


class FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}


def test_fleet_ca_requires_secret(monkeypatch):
    monkeypatch.setattr(settings, "mcp_secret", "fs")
    resp = asyncio.run(control._fleet_ca(FakeRequest(headers={})))
    assert resp.status_code == 401


def test_fleet_ca_missing_file_is_500(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "mcp_secret", "fs")
    monkeypatch.setattr(settings, "tls_ca_cert", tmp_path / "nope.crt")
    resp = asyncio.run(control._fleet_ca(FakeRequest(headers={"x-fleet-secret": "fs"})))
    assert resp.status_code == 500


def test_fleet_ca_returns_der_b64(monkeypatch, tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "nff-fleet-ca")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    ca_path = tmp_path / "ca.crt"
    ca_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    monkeypatch.setattr(settings, "mcp_secret", "fs")
    monkeypatch.setattr(settings, "tls_ca_cert", ca_path)

    resp = asyncio.run(control._fleet_ca(FakeRequest(headers={"x-fleet-secret": "fs"})))
    assert resp.status_code == 200
    data = json.loads(resp.body)
    der = base64.b64decode(data["ca_der_b64"])
    assert der == cert.public_bytes(serialization.Encoding.DER)
    assert data["not_after"]
