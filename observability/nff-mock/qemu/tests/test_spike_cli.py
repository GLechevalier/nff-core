"""The `nff-mock-l2 spike` command + the spike heartbeat detector (pure parts; no QEMU/broker)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from click.testing import CliRunner

from nff_mock_l2.cli import cli

# Load spike/tools/watch_heartbeat.py by path (it lives outside the package, and the test must not
# require paho — the module's paho import is lazy inside main()).
_WATCHER = Path(__file__).resolve().parent.parent / "spike" / "tools" / "watch_heartbeat.py"
_spec = importlib.util.spec_from_file_location("watch_heartbeat", _WATCHER)
watch_heartbeat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watch_heartbeat)


# ── spike --print (offline argv) ──────────────────────────────────────────────


def test_spike_print_argv_shape(monkeypatch):
    monkeypatch.setenv("NFF_MOCK_DEVICE_ID", "spike-01")
    monkeypatch.setenv("NFF_MOCK_PROJECT_ID", "proj-uuid")
    result = CliRunner().invoke(cli, ["spike", "--print", "--image", "/tmp/merged.bin"])
    assert result.exit_code == 0, result.output
    line = [ln for ln in result.output.splitlines() if not ln.startswith("#")][-1]
    # split() is lossy for the guestfwd (it contains spaces), so assert space-free positional
    # tokens via the argv and the guestfwd/nic as substrings of the whole line.
    argv = line.split()
    assert argv[argv.index("-machine") + 1] == "esp32"
    assert argv[argv.index("-drive") + 1] == "file=/tmp/merged.bin,if=mtd,format=raw"
    assert "-nic user,model=open_eth,id=net0," in line
    assert "guestfwd=tcp:10.13.0.10:8883-cmd:ncat nff-fleet 8883" in line
    # The single spike boot must persist — no -no-reboot (that's the relaunch-loop's flag).
    assert "-no-reboot" not in argv


def test_spike_without_image_prints_only(monkeypatch):
    """No --image ⇒ print the command and exit 0 (offline check), never try to launch QEMU."""
    monkeypatch.setenv("NFF_MOCK_DEVICE_ID", "spike-01")
    monkeypatch.setenv("NFF_MOCK_PROJECT_ID", "proj-uuid")
    result = CliRunner().invoke(cli, ["spike"])
    assert result.exit_code == 0, result.output
    assert "-machine esp32" in result.output


def test_spike_falls_back_to_placeholder_identity(monkeypatch):
    """Like `doctor`: with no device id in env the command still builds a valid argv."""
    monkeypatch.delenv("NFF_MOCK_DEVICE_ID", raising=False)
    monkeypatch.delenv("NFF_MOCK_PROJECT_ID", raising=False)
    result = CliRunner().invoke(cli, ["spike", "--print"])
    assert result.exit_code == 0, result.output
    assert "model=open_eth" in result.output


# ── watch_heartbeat.heartbeat_matches (pure) ──────────────────────────────────


def test_heartbeat_matches_online():
    assert watch_heartbeat.heartbeat_matches(
        "nff/proj-uuid/devices/spike-01/heartbeat", b'{"status":"online","id":"spike-01"}', "spike-01"
    )


def test_heartbeat_matches_defaults_to_online_when_status_absent():
    # The router treats a missing status as online (registry.update_heartbeat), so we accept it.
    assert watch_heartbeat.heartbeat_matches(
        "nff/p/devices/spike-01/heartbeat", b'{"id":"spike-01"}', "spike-01"
    )


def test_heartbeat_rejects_offline():
    assert not watch_heartbeat.heartbeat_matches(
        "nff/p/devices/spike-01/heartbeat", b'{"status":"offline"}', "spike-01"
    )


def test_heartbeat_rejects_other_device_and_wrong_topic():
    assert not watch_heartbeat.heartbeat_matches(
        "nff/p/devices/other/heartbeat", b'{"status":"online"}', "spike-01"
    )
    assert not watch_heartbeat.heartbeat_matches(
        "nff/p/devices/spike-01/crash", b'{"status":"online"}', "spike-01"
    )
    # Stale unscoped schema must not match the 5-part project-scoped check.
    assert not watch_heartbeat.heartbeat_matches(
        "nff/devices/spike-01/heartbeat", b'{"status":"online"}', "spike-01"
    )


def test_heartbeat_rejects_bad_json():
    assert not watch_heartbeat.heartbeat_matches(
        "nff/p/devices/spike-01/heartbeat", b"not-json", "spike-01"
    )
