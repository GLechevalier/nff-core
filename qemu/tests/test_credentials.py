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


def test_public_point_from_signing_key_is_65_bytes(tmp_path):
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

    key = ec.generate_private_key(ec.SECP256R1())
    pem = tmp_path / "cmd_sign.pem"
    pem.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))

    point = creds.public_point_from_signing_key(pem)
    assert len(point) == 65  # 0x04 || X(32) || Y(32)
    assert point[0] == 0x04
