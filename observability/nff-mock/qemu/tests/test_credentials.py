"""The NVS credential CSV is assembled correctly (pure; no esp-idf tools needed)."""

from __future__ import annotations

from nff_mock_l2 import credentials as creds
from nff_mock_l2.config import NffMockL2Settings


def test_build_nvs_csv_shape():
    csv = creds.build_nvs_csv(
        strings={"device_id": "mock-l2-01", "broker_host": "10.13.0.10"},
        files={"device_crt": "/data/certs/mock-l2-01/device.crt"},
    )
    lines = csv.strip().splitlines()
    assert lines[0] == "key,type,encoding,value"
    assert lines[1] == "nff,namespace,,"
    assert "device_id,data,string,mock-l2-01" in lines
    assert "broker_host,data,string,10.13.0.10" in lines
    assert "device_crt,file,binary,/data/certs/mock-l2-01/device.crt" in lines


def test_csv_escapes_commas():
    csv = creds.build_nvs_csv(strings={"k": "a,b"}, files={})
    assert 'k,data,string,"a,b"' in csv


def test_csv_for_device_uses_provisioned_cert_paths_and_guest_broker():
    cfg = NffMockL2Settings(
        device_id="mock-l2-01", project_id="proj-9", guest_broker_ip="10.13.0.10"
    )
    csv = creds.nvs_csv_for_device(cfg, boot_epoch=1750000000)
    # identity + the QEMU-side broker IP (not the service name) + the injected clock
    assert "device_id,data,string,mock-l2-01" in csv
    assert "project_id,data,string,proj-9" in csv
    assert "broker_host,data,string,10.13.0.10" in csv
    assert "boot_epoch,data,string,1750000000" in csv
    # certs come from the same shared layout the L1 mock reads (NffMockCoreSettings paths)
    assert str(cfg.device_cert) in csv
    assert str(cfg.ca_cert) in csv
    # a tenant device also ships the project intermediate CA
    assert str(cfg.project_ca_cert) in csv
    # without cmd_vkey_path the command-verify key is omitted (connect+heartbeat still work)
    assert "cmd_vkey" not in csv


def test_csv_includes_cmd_vkey_when_path_given():
    cfg = NffMockL2Settings(
        device_id="mock-l2-01", project_id="proj-9", guest_broker_ip="10.13.0.10"
    )
    csv = creds.nvs_csv_for_device(
        cfg, boot_epoch=1750000000, cmd_vkey_path="/run/qemu/cmd_vkey.bin"
    )
    assert "cmd_vkey,file,binary,/run/qemu/cmd_vkey.bin" in csv


def test_materialize_der_creds_converts_pem_to_der(tmp_path):
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    certs = tmp_path / "certs"
    dev = certs / "dev-x"
    dev.mkdir(parents=True)
    key = ec.generate_private_key(ec.SECP256R1())
    # fleet provisions a SEC1 "EC PRIVATE KEY" PEM — the exact case the SDK's PKCS#8 wrapping rejects
    (dev / "device.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "dev-x")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc))
        .not_valid_after(datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    (dev / "device.crt").write_bytes(cert_pem)
    (certs / "ca.crt").write_bytes(cert_pem)

    cfg = NffMockL2Settings(device_id="dev-x", project_id=None, certs_dir=certs)
    out = creds.materialize_der_creds(cfg, tmp_path / "der")

    # certs come back as parseable X.509 DER; the key as PKCS#8 DER (SEC1 was re-encoded)
    x509.load_der_x509_certificate((tmp_path / "der" / f"{creds.KEY_DEVICE_CERT}.der").read_bytes())
    key_der = (tmp_path / "der" / f"{creds.KEY_DEVICE_KEY}.der").read_bytes()
    loaded = serialization.load_der_private_key(key_der, password=None)
    assert isinstance(loaded, ec.EllipticCurvePrivateKey)
    assert out[creds.KEY_DEVICE_KEY].endswith(".der")


def test_public_point_from_signing_key_is_65_bytes(tmp_path):
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

    key = ec.generate_private_key(ec.SECP256R1())
    pem = tmp_path / "cmd_sign.pem"
    pem.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))

    point = creds.public_point_from_signing_key(pem)
    assert len(point) == 65  # 0x04 || X(32) || Y(32)
    assert point[0] == 0x04
