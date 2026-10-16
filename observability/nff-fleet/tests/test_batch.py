"""Shared batch bootstrap credential (DEVICE_OWNERSHIP_DESIGN.md §8)."""
import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

from nff_fleet import signing
from nff_fleet.provisioning import ca
from nff_fleet.provisioning import batch

PROJ = "proj-batch-1"
BATCH = "batch-abc"
DEV = "dev-batch-1"


@pytest.fixture
def fleet_ca(tmp_path, monkeypatch):
    monkeypatch.setattr(ca.settings, "data_dir", tmp_path)
    ca.clear_cache()
    signing.clear_cache()
    ca.generate_ca()
    yield tmp_path
    ca.clear_cache()
    signing.clear_cache()


def test_batch_cert_issue_detect_and_extract(fleet_ca):
    cert_path, key_path = batch.issue_batch_cert(PROJ, BATCH)
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    assert batch.is_batch_cert(cert) is True
    assert batch.batch_id_from_cert(cert) == BATCH
    assert ca.project_id_from_cert(cert) == PROJ          # carries project too
    assert cert.subject.rfc4514_string() == f"CN=batch:{BATCH}"


def test_operational_cert_is_not_a_batch_cert(fleet_ca):
    cert_path, _ = ca.issue_device_cert(DEV, PROJ)
    leaf = x509.load_pem_x509_certificate(cert_path.read_bytes())
    assert batch.is_batch_cert(leaf) is False
    assert batch.batch_id_from_cert(leaf) is None


def test_batch_cert_chains_to_project_intermediate(fleet_ca):
    cert_path, _ = batch.issue_batch_cert(PROJ, BATCH)
    bcert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    inter = x509.load_pem_x509_certificate((ca.settings.project_keys_dir / PROJ / "ca.crt").read_bytes())
    inter.public_key().verify(
        bcert.signature, bcert.tbs_certificate_bytes, ec.ECDSA(bcert.signature_hash_algorithm)
    )
    assert batch.batch_cert_serial(BATCH) == format(bcert.serial_number, "x")
