"""credentials.h carries the chain (§5 A1) and the batch bootstrap header carries claim material (§8)."""
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from nff_fleet import signing
from nff_fleet.provisioning import ca, batch, credentials

PROJ = "proj-cred-1"
BATCH = "batch-cred"
DEV = "dev-cred-1"


@pytest.fixture
def fleet_ca(tmp_path, monkeypatch):
    monkeypatch.setattr(ca.settings, "data_dir", tmp_path)
    monkeypatch.setattr(ca.settings, "tls_ca_cert", tmp_path / "certs" / "ca.crt")
    ca.clear_cache()
    signing.clear_cache()
    ca.generate_ca()
    yield tmp_path
    ca.clear_cache()
    signing.clear_cache()


def test_operational_header_embeds_chain_and_root(fleet_ca):
    signing.ensure_project_key(PROJ)
    ca.issue_device_cert(DEV, PROJ)
    h = credentials.render_credentials_h(DEV)
    assert "NFF_INTERMEDIATE_CERT_DER" in h
    assert "NFF_CLIENT_CERT_DER" in h
    assert "NFF_CMD_VERIFY_KEY_DER" in h
    assert f'#define NFF_PROJECT_ID "{PROJ}"' in h

    # NFF_CA_CERT_DER must be the ROOT (device pins root to verify the broker server cert).
    root_der = x509.load_pem_x509_certificate(
        (ca.settings.certs_dir / "ca.crt").read_bytes()
    ).public_bytes(serialization.Encoding.DER)
    first_root_byte = f"0x{root_der[0]:02x}, 0x{root_der[1]:02x}"
    assert first_root_byte in h  # root bytes appear (in the NFF_CA_CERT_DER array)


def test_batch_bootstrap_header_has_claim_material(fleet_ca):
    signing.ensure_project_key(PROJ)
    signing.ensure_rollover_key()
    batch.issue_batch_cert(PROJ, BATCH)
    h = credentials.render_batch_bootstrap_h(PROJ, BATCH)
    for token in (
        "#define NFF_BOOTSTRAP 1",
        f'#define NFF_PROJECT_ID "{PROJ}"',
        f'#define NFF_BATCH_ID "{BATCH}"',
        "NFF_CLIENT_CERT_DER",
        "NFF_INTERMEDIATE_CERT_DER",
        "NFF_CLIENT_KEY_DER",
        "NFF_CA_CERT_DER",
        "NFF_ROLLOVER_VERIFY_KEY_DER",
    ):
        assert token in h, token
