"""Per-project intermediate CA (DEVICE_OWNERSHIP_DESIGN.md §5 Option A1).

A device leaf is now signed by the project's intermediate CA, which is signed by the root, so the
chain is leaf → intermediate → root. The project is still read from the signed NFF_PROJECT_OID leaf
extension.
"""
import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

from nff_fleet import signing
from nff_fleet.provisioning import ca

PROJ = "proj-ca-1"
DEV = "dev-ca-1"


@pytest.fixture
def fleet_ca(tmp_path, monkeypatch):
    monkeypatch.setattr(ca.settings, "data_dir", tmp_path)
    ca.clear_cache()
    signing.clear_cache()
    ca.generate_ca()
    yield tmp_path
    ca.clear_cache()
    signing.clear_cache()


def _verify(parent: x509.Certificate, child: x509.Certificate) -> None:
    parent.public_key().verify(
        child.signature, child.tbs_certificate_bytes, ec.ECDSA(child.signature_hash_algorithm)
    )


def test_ensure_project_ca_idempotent(fleet_ca):
    c1, k1 = ca.ensure_project_ca(PROJ)
    body1 = c1.read_bytes()
    c2, k2 = ca.ensure_project_ca(PROJ)
    assert (c1, k1) == (c2, k2)
    assert c2.read_bytes() == body1  # not regenerated


def test_intermediate_is_a_capped_ca(fleet_ca):
    cert_path, _ = ca.ensure_project_ca(PROJ)
    inter = x509.load_pem_x509_certificate(cert_path.read_bytes())
    bc = inter.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert bc.ca is True
    assert bc.path_length == 0  # may sign leaves, not further CAs
    root = x509.load_pem_x509_certificate((ca.settings.certs_dir / "ca.crt").read_bytes())
    _verify(root, inter)  # root signed the intermediate


def test_server_cert_always_carries_fixed_dns_sans(fleet_ca):
    # Real-silicon firmware dials mqtts://nff-fleet-server:8883 and mbedtls matches the
    # hostname against dNSName SANs ONLY (never the CN) — dropping this SAN breaks every
    # real board while all mocks (which skip hostname checks) keep passing. Regression
    # guard for the prod outage where the cert had every SAN except this one.
    cert_path, _ = ca.issue_server_cert("192.0.2.7")
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    dns = set(san.get_values_for_type(x509.DNSName))
    assert {"nff-fleet-server", "nff-fleet", "localhost", "192.0.2.7", "127.0.0.1"} <= dns


def test_device_cert_signed_by_intermediate_and_carries_project(fleet_ca):
    cert_path, _ = ca.issue_device_cert(DEV, PROJ)
    leaf = x509.load_pem_x509_certificate(cert_path.read_bytes())
    inter = x509.load_pem_x509_certificate((ca.settings.project_keys_dir / PROJ / "ca.crt").read_bytes())
    root = x509.load_pem_x509_certificate((ca.settings.certs_dir / "ca.crt").read_bytes())

    # Full chain validates: leaf <- intermediate <- root.
    _verify(inter, leaf)
    _verify(root, inter)
    # Belt-and-suspenders: the project is still in the signed leaf extension.
    assert ca.project_id_from_cert(leaf) == PROJ
    assert ca.device_id_from_cert(leaf) == DEV
    # The leaf is NOT directly signed by the root anymore.
    with pytest.raises(Exception):
        _verify(root, leaf)
