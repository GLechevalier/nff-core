"""The NVS credential splice: partition generation + writing it into the flash image.

The pure CSV assembly is covered in test_credentials.py; here we cover the Stage-5 wiring —
write_nvs_csv threading der_dir/cmd_vkey_path, generate_nvs_bin's orchestration (with the esp-idf
tool + real crypto stubbed), and the in-place flash splice — none of which needs a real
qemu/nvs_partition_gen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nff_mock_l2 import credentials as creds
from nff_mock_l2.config import NffMockL2Settings


def _cfg(**kw) -> NffMockL2Settings:
    kw.setdefault("device_id", "mock-l2-01")
    kw.setdefault("project_id", "proj-9")
    return NffMockL2Settings(**kw)


def test_write_nvs_csv_threads_der_dir_and_cmd_vkey(tmp_path, monkeypatch):
    seen = {}

    def fake_assemble(cfg, *, boot_epoch, cmd_vkey_path=None, der_dir=None):
        seen["boot_epoch"] = boot_epoch
        seen["cmd_vkey_path"] = cmd_vkey_path
        seen["der_dir"] = der_dir
        return "key,type,encoding,value\n"

    monkeypatch.setattr(creds, "nvs_csv_for_device", fake_assemble)
    dest = creds.write_nvs_csv(
        _cfg(), boot_epoch=42, dest=tmp_path / "n.csv",
        der_dir=tmp_path / "der", cmd_vkey_path=tmp_path / "vkey.bin",
    )
    assert dest.read_text().startswith("key,type,encoding,value")
    assert seen == {"boot_epoch": 42, "cmd_vkey_path": tmp_path / "vkey.bin",
                    "der_dir": tmp_path / "der"}


def test_generate_nvs_bin_orchestrates_with_cmd_vkey(tmp_path, monkeypatch):
    # A present signing key → the 65-byte public point is materialised and the CSV references it.
    key = tmp_path / "cmd_sign.pem"
    key.write_bytes(b"unused - public_point is stubbed")
    cfg = _cfg(cmd_verify_key=key)

    monkeypatch.setattr(creds, "public_point_from_signing_key", lambda p: b"\x04" + b"\x11" * 64)

    csv_calls = {}

    def fake_write_csv(cfg, *, boot_epoch, dest, der_dir=None, cmd_vkey_path=None):
        csv_calls["der_dir"] = der_dir
        csv_calls["cmd_vkey_path"] = cmd_vkey_path
        Path(dest).write_text("csv")
        return Path(dest)

    monkeypatch.setattr(creds, "write_nvs_csv", fake_write_csv)

    ran = {}

    def fake_run(cmd, check):
        ran["cmd"] = cmd
        ran["check"] = check
        Path(cmd[-2]).write_bytes(b"\x00" * 0x6000)  # the generated partition

    monkeypatch.setattr(creds.subprocess, "run", fake_run)

    out = creds.generate_nvs_bin(cfg, boot_epoch=1000, work_dir=tmp_path / "work")

    # cmd_vkey blob was derived + written, and the CSV was told to reference it + the DER dir.
    vkey = tmp_path / "work" / "cmd_vkey.bin"
    assert vkey.read_bytes()[0] == 0x04 and len(vkey.read_bytes()) == 65
    assert csv_calls["cmd_vkey_path"] == vkey
    assert csv_calls["der_dir"] == tmp_path / "work" / "der"
    # nvs_partition_gen was invoked: generate <csv> <out> <hex size>
    assert ran["check"] is True
    assert ran["cmd"][-4:] == ["generate", str(tmp_path / "work" / "nff_nvs.csv"),
                               str(out), hex(cfg.nvs_size)]
    assert out == tmp_path / "work" / "nff_nvs.bin"


def test_generate_nvs_bin_skips_cmd_vkey_when_key_absent(tmp_path, monkeypatch):
    cfg = _cfg(cmd_verify_key=tmp_path / "does-not-exist.pem")

    def boom(_p):  # must NOT be called when the key file is missing
        raise AssertionError("public_point_from_signing_key called without a key file")

    monkeypatch.setattr(creds, "public_point_from_signing_key", boom)

    csv_calls = {}

    def fake_write_csv(cfg, *, boot_epoch, dest, der_dir=None, cmd_vkey_path=None):
        csv_calls["cmd_vkey_path"] = cmd_vkey_path
        Path(dest).write_text("csv")
        return Path(dest)

    monkeypatch.setattr(creds, "write_nvs_csv", fake_write_csv)
    monkeypatch.setattr(creds.subprocess, "run",
                        lambda cmd, check: Path(cmd[-2]).write_bytes(b""))

    creds.generate_nvs_bin(cfg, boot_epoch=1, work_dir=tmp_path / "w")
    assert csv_calls["cmd_vkey_path"] is None
    assert not (tmp_path / "w" / "cmd_vkey.bin").exists()


def test_splice_writes_at_offset_and_leaves_neighbours(tmp_path):
    flash = tmp_path / "flash.bin"
    flash.write_bytes(b"\xff" * 0x10000)  # 64 KiB dummy image
    nvs = tmp_path / "nvs.bin"
    nvs.write_bytes(b"\xAB" * 0x6000)

    creds.splice_nvs_into_flash(flash, nvs, offset=0x9000, size_limit=0x6000)

    data = flash.read_bytes()
    assert data[0x9000:0x9000 + 0x6000] == b"\xAB" * 0x6000
    assert data[:0x9000] == b"\xff" * 0x9000                 # bootloader/part-table region untouched
    assert data[0x9000 + 0x6000:] == b"\xff" * (0x10000 - 0xF000)  # region after nvs untouched


def test_splice_rejects_oversized_blob(tmp_path):
    flash = tmp_path / "flash.bin"
    flash.write_bytes(b"\x00" * 0x10000)
    nvs = tmp_path / "nvs.bin"
    nvs.write_bytes(b"\x01" * (0x6000 + 1))
    with pytest.raises(ValueError, match="exceeds the nvs partition"):
        creds.splice_nvs_into_flash(flash, nvs, offset=0x9000, size_limit=0x6000)
