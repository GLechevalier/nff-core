"""The qemu-system-xtensa argv is built correctly (pure; no QEMU needed)."""

from __future__ import annotations

from nff_mock_l2 import qemu
from nff_mock_l2.config import NffMockL2Settings


def _cfg(**kw) -> NffMockL2Settings:
    base = dict(device_id="mock-l2-01", project_id="proj", broker_host="nff-fleet", broker_port=8883)
    base.update(kw)
    return NffMockL2Settings(**base)


def test_argv_has_machine_and_flash_drive():
    cfg = _cfg()
    argv = qemu.build_qemu_argv(cfg, "/run/qemu/flash.bin")
    assert argv[0] == cfg.qemu_path
    # -machine esp32
    assert "-machine" in argv and argv[argv.index("-machine") + 1] == "esp32"
    # the writable flash is mounted as the MTD drive
    drive = argv[argv.index("-drive") + 1]
    assert drive == "file=/run/qemu/flash.bin,if=mtd,format=raw"
    # UART0 to stdout for the supervisor to scan
    assert argv[argv.index("-serial") + 1] == "mon:stdio"


def test_argv_bridges_broker_via_guestfwd():
    cfg = _cfg(broker_host="nff-fleet", broker_port=8883, guest_broker_ip="10.13.0.10")
    argv = qemu.build_qemu_argv(cfg, "/run/qemu/flash.bin")
    nic = argv[argv.index("-nic") + 1]
    # openeth is the only NIC the esp32 machine emulates
    assert "model=open_eth" in nic
    # the SLIRP subnet must contain the guest broker IP or QEMU rejects the guestfwd (live-run fix)
    assert "net=10.13.0.0/24" in nic
    # the firmware's broker IP is forwarded host-side to the real broker
    assert "guestfwd=tcp:10.13.0.10:8883-cmd:ncat nff-fleet 8883" in nic


def test_argv_adds_ota_guestfwd_only_when_configured():
    without = qemu.build_qemu_argv(_cfg(), "/f.bin")
    assert "10.13.0.20" not in without[without.index("-nic") + 1]

    with_ota = qemu.build_qemu_argv(_cfg(ota_host="ota.example", ota_port=443), "/f.bin")
    nic = with_ota[with_ota.index("-nic") + 1]
    assert "guestfwd=tcp:10.13.0.20:443-cmd:ncat ota.example 443" in nic


def test_no_reboot_toggle():
    assert "-no-reboot" in qemu.build_qemu_argv(_cfg(), "/f.bin", no_reboot=True)
    assert "-no-reboot" not in qemu.build_qemu_argv(_cfg(), "/f.bin", no_reboot=False)


def test_qemu_mem_optional():
    assert "-m" not in qemu.build_qemu_argv(_cfg(qemu_mem=""), "/f.bin")
    argv = qemu.build_qemu_argv(_cfg(qemu_mem="4M"), "/f.bin")
    assert argv[argv.index("-m") + 1] == "4M"
