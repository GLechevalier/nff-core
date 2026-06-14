"""Tests for the event-safety sandbox: the allowlist guard and its wiring."""

import sys
from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import NMAP_XML_ONE_HOST, make_cmd

from nff_pentester.config import settings
from nff_pentester.utils.sandbox import SandboxViolation, assert_target_allowed

# The allowlist used across the guard tests (loopback + an isolated subnet).
_ALLOWLIST = ["127.0.0.0/8", "192.168.8.0/24"]

_PERMITTED = ["127.0.0.1", "192.168.8.50", "http://192.168.8.1/login", "192.168.8.0/24"]
# Off-allowlist targets that need no DNS (bare IPs and a too-broad CIDR).
_BLOCKED = ["192.168.1.1", "8.8.8.8", "192.168.0.0/16"]


@pytest.fixture
def demo_on(monkeypatch):
    """Turn the sandbox on with the standard test allowlist."""
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "target_allowlist", list(_ALLOWLIST))


# ── Guard unit tests ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("target", _PERMITTED)
def test_guard_permits_allowlisted(demo_on, target):
    # Should not raise.
    assert_target_allowed(target)


@pytest.mark.parametrize("target", _BLOCKED)
def test_guard_blocks_offlist(demo_on, target):
    with pytest.raises(SandboxViolation):
        assert_target_allowed(target)


def test_guard_blocks_unresolved_hostname(demo_on):
    # example.com resolves to a public IP → outside the allowlist → refused.
    fake = [(2, 1, 6, "", ("93.184.216.34", 0))]
    with patch("nff_pentester.utils.sandbox.socket.getaddrinfo", return_value=fake):
        with pytest.raises(SandboxViolation):
            assert_target_allowed("example.com")


def test_guard_permits_hostname_resolving_into_allowlist(demo_on):
    fake = [(2, 1, 6, "", ("127.0.0.1", 0))]
    with patch("nff_pentester.utils.sandbox.socket.getaddrinfo", return_value=fake):
        assert_target_allowed("localhost.demo")  # should not raise


def test_guard_refuses_unresolvable_name(demo_on):
    import socket as _socket

    with patch(
        "nff_pentester.utils.sandbox.socket.getaddrinfo",
        side_effect=_socket.gaierror("name resolution failed"),
    ):
        with pytest.raises(SandboxViolation):
            assert_target_allowed("nonexistent.invalid")


@pytest.mark.parametrize("target", _PERMITTED + _BLOCKED + ["example.com"])
def test_guard_is_noop_when_demo_off(monkeypatch, target):
    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(settings, "target_allowlist", list(_ALLOWLIST))
    # No raise, no DNS — every target passes through untouched.
    assert_target_allowed(target)


def test_sandbox_violation_is_value_error():
    # Tools catch ValueError; SandboxViolation must be one so the wiring works.
    assert issubclass(SandboxViolation, ValueError)


# ── Tool wiring: the guard actually stops the tool before any I/O ───────────────

async def test_port_scan_blocks_offlist_target_no_subprocess(demo_on, mock_ctx):
    from nff_pentester.tools.network.scanner import PortScanInput, port_scan

    run = AsyncMock(return_value=make_cmd(stdout=NMAP_XML_ONE_HOST))
    with patch("nff_pentester.tools.network.scanner.run_command", new=run):
        result = await port_scan(PortScanInput(target="8.8.8.8", ports="80"), mock_ctx)

    assert result.error is not None
    assert result.host_up is False
    run.assert_not_called()  # no process spawned for an off-allowlist target


async def test_port_scan_allows_onlist_target(demo_on, mock_ctx):
    from nff_pentester.tools.network.scanner import PortScanInput, port_scan

    run = AsyncMock(return_value=make_cmd(stdout=NMAP_XML_ONE_HOST))
    with patch("nff_pentester.tools.network.scanner.run_command", new=run):
        result = await port_scan(PortScanInput(target="127.0.0.1", ports="80"), mock_ctx)

    run.assert_called_once()  # on-allowlist target proceeds normally
    assert result.host_up is True


async def test_mqtt_probe_blocks_offlist_target(demo_on, mock_ctx):
    from nff_pentester.tools.protocols.mqtt import MqttProbeInput, mqtt_probe

    result = await mqtt_probe(MqttProbeInput(host="8.8.8.8"), mock_ctx)
    assert result.connected is False
    assert result.error is not None
    assert "allowlist" in result.error


async def test_web_tool_blocks_offlist_url(demo_on, mock_ctx):
    from nff_pentester.tools.web.fuzzer import FuzzWebPathsInput, fuzz_web_paths

    result = await fuzz_web_paths(
        FuzzWebPathsInput(base_url="http://8.8.8.8", wordlist=["admin"]), mock_ctx
    )
    assert result.error is not None
    assert result.hits == []
    assert result.paths_tested == 0


# ── BLE demo short-circuit (no radio) ───────────────────────────────────────────

async def test_ble_scan_demo_fixture_no_radio(demo_on, mock_ctx):
    from nff_pentester.tools.protocols.ble import BleScanInput, ble_scan

    # bleak forced absent — proves the fixture path never touches the radio.
    with patch.dict(sys.modules, {"bleak": None}):
        result = await ble_scan(BleScanInput(scan_seconds=1), mock_ctx)

    assert result.error is None
    assert result.device_count >= 1
    assert any("SmartLock" in (d.name or "") for d in result.devices)


async def test_ble_scan_demo_fixture_honours_filter(demo_on, mock_ctx):
    from nff_pentester.tools.protocols.ble import BleScanInput, ble_scan

    result = await ble_scan(BleScanInput(scan_seconds=1, filter_name="SmartLock"), mock_ctx)
    assert result.device_count == 1
    assert "SmartLock" in (result.devices[0].name or "")


# ── sandbox_status tool ─────────────────────────────────────────────────────────

async def test_sandbox_status_reports_state(demo_on, mock_ctx):
    from nff_pentester.tools.safety.status import sandbox_status

    result = await sandbox_status(mock_ctx)
    assert result.demo_mode is True
    assert result.target_allowlist == _ALLOWLIST
