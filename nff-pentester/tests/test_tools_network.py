"""Unit tests for network tools: port_scan and discover_devices."""

from unittest.mock import AsyncMock, patch

from tests.conftest import NMAP_XML_ONE_HOST, make_cmd

from nff_pentester.tools.network.scanner import (
    DeviceDiscoveryInput,
    PortScanInput,
    discover_devices,
    port_scan,
)

_PATCH_RUN = "nff_pentester.tools.network.scanner.run_command"


# ── port_scan ──────────────────────────────────────────────────────────────────

async def test_port_scan_rejects_injection(mock_ctx):
    params = PortScanInput(target="192.168.1.1; rm -rf /", ports="80")
    result = await port_scan(params, mock_ctx)
    assert result.error is not None
    assert result.ports == []
    assert result.host_up is False


async def test_port_scan_nmap_not_found(mock_ctx):
    with patch(_PATCH_RUN, new=AsyncMock(return_value=make_cmd(returncode=1, stderr="nmap: not found"))):
        params = PortScanInput(target="192.168.1.1", ports="80")
        result = await port_scan(params, mock_ctx)
    assert result.error is not None
    assert result.ports == []


async def test_port_scan_timeout(mock_ctx):
    with patch(_PATCH_RUN, new=AsyncMock(return_value=make_cmd(timed_out=True, elapsed=120.0))):
        params = PortScanInput(target="192.168.1.1", ports="1-1024")
        result = await port_scan(params, mock_ctx)
    assert result.timed_out is True
    assert result.ports == []


async def test_port_scan_parses_xml_output(mock_ctx):
    with patch(_PATCH_RUN, new=AsyncMock(return_value=make_cmd(stdout=NMAP_XML_ONE_HOST))):
        params = PortScanInput(target="192.168.1.1", ports="80")
        result = await port_scan(params, mock_ctx)
    assert result.host_up is True
    assert result.open_port_count == 1
    assert result.target == "192.168.1.1"
    port = result.ports[0]
    assert port.port == 80
    assert port.state == "open"
    assert port.service == "http"
    assert "nginx" in (port.version or "")


async def test_port_scan_udp_adds_su_flag(mock_ctx):
    mock_run = AsyncMock(return_value=make_cmd(stdout=NMAP_XML_ONE_HOST))
    with patch(_PATCH_RUN, new=mock_run):
        params = PortScanInput(target="192.168.1.1", ports="5683", scan_type="udp")
        await port_scan(params, mock_ctx)
    args = mock_run.call_args.args[0]
    assert "-sU" in args


async def test_port_scan_tcp_omits_su_flag(mock_ctx):
    mock_run = AsyncMock(return_value=make_cmd(stdout=NMAP_XML_ONE_HOST))
    with patch(_PATCH_RUN, new=mock_run):
        params = PortScanInput(target="192.168.1.1", ports="80")
        await port_scan(params, mock_ctx)
    args = mock_run.call_args.args[0]
    assert "-sU" not in args


async def test_port_scan_no_hosts_in_output(mock_ctx):
    empty_xml = '<?xml version="1.0"?><nmaprun><runstats><finished elapsed="0.1"/></runstats></nmaprun>'
    with patch(_PATCH_RUN, new=AsyncMock(return_value=make_cmd(stdout=empty_xml))):
        params = PortScanInput(target="10.0.0.1", ports="80")
        result = await port_scan(params, mock_ctx)
    assert result.host_up is False
    assert result.open_port_count == 0


# ── discover_devices ───────────────────────────────────────────────────────────

async def test_discover_devices_rejects_injection(mock_ctx):
    params = DeviceDiscoveryInput(subnet="10.0.0.0/24 && id")
    result = await discover_devices(params, mock_ctx)
    assert result.error is not None
    assert result.devices == []


async def test_discover_devices_timeout(mock_ctx):
    with patch(_PATCH_RUN, new=AsyncMock(return_value=make_cmd(timed_out=True, elapsed=120.0))):
        params = DeviceDiscoveryInput(subnet="192.168.1.0/24")
        result = await discover_devices(params, mock_ctx)
    assert result.timed_out is True
    assert result.device_count == 0


async def test_discover_devices_parses_hosts(mock_ctx):
    with patch(_PATCH_RUN, new=AsyncMock(return_value=make_cmd(stdout=NMAP_XML_ONE_HOST))):
        params = DeviceDiscoveryInput(subnet="192.168.1.0/24")
        result = await discover_devices(params, mock_ctx)
    assert result.device_count == 1
    device = result.devices[0]
    assert device.ip == "192.168.1.1"
    assert device.vendor == "TP-Link"
    assert device.mac == "AA:BB:CC:DD:EE:FF"
