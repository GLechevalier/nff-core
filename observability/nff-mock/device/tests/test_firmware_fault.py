"""Held-firmware fault drives the crash-loop.

A mock makes the `fault` declared in the firmware it holds the source of truth for
crash-looping: a buggy sketch arms the persistent fault, a fixed sketch clears it, and a
command-injected fault is never clobbered by a held-firmware sketch that has none.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from nff_mock_core.nvs import Nvs

from nff_mock.crash import CrashInjector
from nff_mock.device import VirtualDevice
from nff_mock.firmware import load_firmware


def _sketch(version: str, *, fault: str | None = None) -> bytes:
    spec: dict = {"version": version}
    if fault:
        spec["fault"] = {"scenario": fault}
    return json.dumps(spec).encode("utf-8")


class _FakeDevice:
    """Just enough of a VirtualDevice for _sync_fault_from_firmware: an NVS + a firmware +
    a CrashInjector. The method touches nothing else."""

    def __init__(self, firmware) -> None:
        self.nvs = Nvs()
        self.firmware = firmware
        self.crash = CrashInjector(self)


# ── loader: fault parsing ────────────────────────────────────────────────────────


def test_loader_parses_known_fault():
    fw = load_firmware(_sketch("5.0.0", fault="null_deref"), fallback_version="1.0.0")
    assert fw.fault == {"scenario": "null_deref"}


def test_loader_no_fault_is_none():
    fw = load_firmware(_sketch("5.0.0"), fallback_version="1.0.0")
    assert fw.fault is None


def test_loader_unknown_fault_ignored():
    fw = load_firmware(_sketch("5.0.0", fault="banana"), fallback_version="1.0.0")
    assert fw.fault is None


def test_default_firmware_has_no_fault():
    fw = load_firmware(None, fallback_version="1.0.0")
    assert getattr(fw, "fault", None) is None


# ── sync: held code arms / clears the crash-loop ─────────────────────────────────


def test_buggy_firmware_arms_fault():
    dev = _FakeDevice(load_firmware(_sketch("5.0.0", fault="null_deref"), fallback_version="1.0.0"))
    VirtualDevice._sync_fault_from_firmware(dev)
    assert dev.crash.fault_scenario == "null_deref"
    assert dev.crash.fault_source == "firmware"


def test_fixed_firmware_clears_firmware_fault():
    dev = _FakeDevice(load_firmware(_sketch("5.0.0", fault="null_deref"), fallback_version="1.0.0"))
    VirtualDevice._sync_fault_from_firmware(dev)
    # Adopt a fixed sketch (no fault) — recovery.
    dev.firmware = load_firmware(_sketch("5.0.1"), fallback_version="1.0.0")
    VirtualDevice._sync_fault_from_firmware(dev)
    assert dev.crash.fault_scenario is None


def test_injected_fault_survives_clean_firmware_sync():
    """A `crash` command armed an injected fault; a held sketch with no fault must NOT clear
    it (its cure is the version gate, not held-code content)."""
    dev = _FakeDevice(load_firmware(_sketch("5.0.0"), fallback_version="1.0.0"))
    dev.crash.arm_fault("null_deref", fixed_after="5.0.0", source="inject")
    VirtualDevice._sync_fault_from_firmware(dev)
    assert dev.crash.fault_scenario == "null_deref"
    assert dev.crash.fault_source == "inject"


# ── heartbeat reflects the crash-loop so the dashboard shows the device broken ───


class _HeartbeatDevice:
    """Enough of a VirtualDevice for build_online + _online_heartbeat: a config, telemetry,
    firmware extras, and a CrashInjector over a real NVS."""

    def __init__(self) -> None:
        self.nvs = Nvs()
        self.fw_version = "5.0.0"
        self.build_id = "abc123"
        self.cfg = SimpleNamespace(
            device_id="mock-test", device_type="esp32", fqbn="esp32:esp32:esp32",
            chip="ESP32", rev=1, flash=4194304, heartbeat_interval_s=30,
            ip="", project_id="p1", name="Mock Test",
        )
        self.telemetry = SimpleNamespace(free_heap=100000, uptime_ms=1234, rssi=-50)
        self.firmware = SimpleNamespace(extra_heartbeat_fields={})
        self.crash = CrashInjector(self)


def test_heartbeat_online_when_healthy():
    dev = _HeartbeatDevice()
    assert VirtualDevice._online_heartbeat(dev)["status"] == "online"


def test_heartbeat_error_while_crash_looping():
    """While a crash-loop fault is armed the device reports 'error' so the fleet flips it red
    until an OTA heals it — without this the mock would heartbeat 'online' the whole time."""
    dev = _HeartbeatDevice()
    dev.crash.arm_fault("null_deref", fixed_after="5.0.0", source="firmware")
    assert VirtualDevice._online_heartbeat(dev)["status"] == "error"
    # Healed: fault cleared → back to online (the dashboard tile goes green).
    dev.crash.clear_fault()
    assert VirtualDevice._online_heartbeat(dev)["status"] == "online"
