"""Supervisor.prepare_flash: stage the writable flash, then splice this device's NVS identity.

The real generate/splice (esp-idf tool + a live flash) is covered structurally in test_splice.py;
here we assert prepare_flash's wiring — it copies the firmware image, guards on the provisioned
certs, and drives the splice with the injected boot_epoch at the configured offset.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nff_mock_l2 import credentials
from nff_mock_l2.config import NffMockL2Settings
from nff_mock_l2.supervisor import Supervisor


def _provisioned_cfg(tmp_path: Path, **kw) -> NffMockL2Settings:
    certs = tmp_path / "certs"
    dev = certs / "mock-l2-01"
    dev.mkdir(parents=True)
    (dev / "device.crt").write_bytes(b"cert")
    (dev / "device.key").write_bytes(b"key")
    (certs / "ca.crt").write_bytes(b"ca")
    image = tmp_path / "stock.bin"
    image.write_bytes(b"FIRMWARE-IMAGE")
    kw.setdefault("firmware_image", image)
    kw.setdefault("flash_path", tmp_path / "run" / "flash.bin")
    return NffMockL2Settings(
        device_id="mock-l2-01", project_id=None, certs_dir=certs, **kw
    )


def test_prepare_flash_copies_then_splices(tmp_path, monkeypatch):
    cfg = _provisioned_cfg(tmp_path)
    calls = {}

    def fake_generate(cfg_, *, boot_epoch, work_dir):
        calls["boot_epoch"] = boot_epoch
        calls["work_dir"] = work_dir
        nvs = Path(work_dir)
        nvs.mkdir(parents=True, exist_ok=True)
        p = nvs / "nff_nvs.bin"
        p.write_bytes(b"NVS")
        return p

    def fake_splice(flash, nvs_bin, *, offset, size_limit):
        calls["flash"] = Path(flash)
        calls["nvs_bin"] = Path(nvs_bin)
        calls["offset"] = offset
        calls["size_limit"] = size_limit

    monkeypatch.setattr(credentials, "generate_nvs_bin", fake_generate)
    monkeypatch.setattr(credentials, "splice_nvs_into_flash", fake_splice)

    sup = Supervisor(cfg, clock=lambda: 1751328000.9)
    flash = sup.prepare_flash()

    # firmware image copied into the writable flash path
    assert flash == cfg.flash_path
    assert flash.read_bytes() == b"FIRMWARE-IMAGE"
    # splice driven with the injected wall-clock boot_epoch (int) at the configured nvs geometry
    assert calls["boot_epoch"] == 1751328000
    assert calls["flash"] == cfg.flash_path
    assert calls["offset"] == cfg.nvs_offset == 0x9000
    assert calls["size_limit"] == cfg.nvs_size == 0x6000


def test_prepare_flash_errors_on_missing_cert(tmp_path, monkeypatch):
    cfg = _provisioned_cfg(tmp_path)
    (cfg.device_key).unlink()  # simulate an unmounted / unprovisioned identity

    monkeypatch.setattr(credentials, "generate_nvs_bin",
                        lambda *a, **k: pytest.fail("splice ran despite a missing cert"))
    sup = Supervisor(cfg)
    with pytest.raises(FileNotFoundError, match="device key"):
        sup.prepare_flash()


def test_prepare_flash_requires_a_firmware_source(tmp_path):
    cfg = _provisioned_cfg(tmp_path, firmware_image=None)
    # no NFF_MOCK_FIRMWARE_IMAGE and no pre-staged flash → the existing loud error
    with pytest.raises(FileNotFoundError, match="no firmware image"):
        Supervisor(cfg).prepare_flash()
