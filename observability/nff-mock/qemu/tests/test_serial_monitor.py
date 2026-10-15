"""Serial marker detection + crash-loop logic (pure; no QEMU needed)."""

from __future__ import annotations

from nff_mock_l2 import serial_monitor as sm


def test_detects_guru_meditation_panic():
    assert sm.looks_like_panic(
        "Guru Meditation Error: Core 1 panic'ed (LoadProhibited). Exception was unhandled."
    )
    assert sm.looks_like_panic("Backtrace: 0x400d1234:0x3ffb1f00 0x400d5678:0x3ffb1f20")
    assert sm.looks_like_panic("assert failed: foo bar.c:42 (x != NULL)")
    assert not sm.looks_like_panic("LED ON")
    assert not sm.looks_like_panic("nff: heartbeat published")


def test_detects_reboot_banners():
    assert sm.looks_like_reboot("ets Jul 29 2019 12:21:46")
    assert sm.looks_like_reboot("rst:0xc (SW_CPU_RESET),boot:0x13")
    assert sm.looks_like_reboot("ESP-ROM:esp32-20190118")
    assert sm.looks_like_reboot("Rebooting...")
    assert not sm.looks_like_reboot("nff core init")


def test_detects_boot_banner():
    assert sm.looks_like_boot_banner("nff core: starting")
    assert sm.looks_like_boot_banner("cpu_start: Pro cpu up.")
    assert not sm.looks_like_boot_banner("rst:0xc (SW_CPU_RESET)")


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_crash_loop_trips_at_threshold_within_window():
    clk = _Clock()
    det = sm.CrashLoopDetector(max_reboots=3, window_s=100.0, clock=clk)
    det.record_reboot()           # t=0
    clk.t = 10.0
    det.record_reboot()
    assert not det.is_crash_looping()  # 2 reboots
    clk.t = 20.0
    det.record_reboot()
    assert det.is_crash_looping()      # 3 reboots within 100s


def test_crash_loop_evicts_old_reboots():
    clk = _Clock()
    det = sm.CrashLoopDetector(max_reboots=3, window_s=100.0, clock=clk)
    det.record_reboot()           # t=0
    clk.t = 200.0
    det.record_reboot()           # t=200 — the t=0 one is now outside the window
    clk.t = 210.0
    det.record_reboot()
    # Only 2 reboots inside the 100s window → not a crash loop.
    assert not det.is_crash_looping()
    assert det.reboot_count == 2
