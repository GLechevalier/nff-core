"""Tests for the newly-implemented tools that close the dead-ended attack chains.

Two layers of coverage:
  * per-tool — each tool produces the exact result field the vuln-tree condition
    reads, set to the activating value (demo fixtures for radio/outbound/physical,
    real static analysis for the offline tools);
  * end-to-end — a synthetic session containing a vulnerable output for every tree
    `tool` makes `walk_vuln_tree` report every terminal chain as *active*. This is the
    executable statement that the missing chains are now covered.
"""

from __future__ import annotations

from unittest.mock import patch

from nff_pentester.config import settings
from nff_pentester.tools.analysis.tree_walker import (
    WalkVulnTreeInput,
    _load_tree,
    walk_vuln_tree,
)
from nff_pentester.tools.components.cve import CveLookupInput, cve_lookup
from nff_pentester.tools.firmware.update_check import (
    UnsignedUpdateCheckInput,
    unsigned_update_check,
)
from nff_pentester.tools.hardware.debug import DebugInterfaceScanInput, debug_interface_scan
from nff_pentester.tools.outbound.analyzer import (
    DnsHijackSimulatorInput,
    OutboundAnalyzerInput,
    TlsValidationTesterInput,
    dns_hijack_simulator,
    outbound_analyzer,
    tls_validation_tester,
)
from nff_pentester.tools.protocols.ble import (
    _DEMO_LOCK_ADDRESS,
    _DEMO_LOCK_CONTROL_UUID,
    GattEnumInput,
    GattWriteInput,
    gatt_enum,
    gatt_write,
)
from nff_pentester.tools.protocols.zigbee import (
    _DEMO_CHANNEL,
    _DEMO_NETWORK_KEY,
    ZigbeeJoinCaptureInput,
    ZigbeeKeyInjectInput,
    ZigbeeScanInput,
    zigbee_join_capture,
    zigbee_key_inject,
    zigbee_scan,
)
from nff_pentester.tools.web.injection import (
    CommandInjectionTesterInput,
    _find_marker,
    command_injection_tester,
)

# ── BLE GATT (demo fixtures) ─────────────────────────────────────────────────


async def test_gatt_enum_demo_finds_writable_no_auth(mock_ctx):
    with patch.object(settings, "demo_mode", True):
        res = await gatt_enum(GattEnumInput(address=_DEMO_LOCK_ADDRESS), mock_ctx)
    assert res.error is None
    assert len(res.writable_no_auth) >= 1  # activates ble_writable_characteristic


async def test_gatt_write_demo_accepts_control_write(mock_ctx):
    with patch.object(settings, "demo_mode", True):
        res = await gatt_write(
            GattWriteInput(address=_DEMO_LOCK_ADDRESS, char_uuid=_DEMO_LOCK_CONTROL_UUID),
            mock_ctx,
        )
    assert res.accepted is True  # activates ble_write_accepted


# ── Zigbee (demo fixtures) ───────────────────────────────────────────────────


async def test_zigbee_scan_demo_finds_network(mock_ctx):
    with patch.object(settings, "demo_mode", True):
        res = await zigbee_scan(ZigbeeScanInput(), mock_ctx)
    assert res.networks_found > 0  # activates zigbee_network_visible


async def test_zigbee_join_capture_demo_captures_key(mock_ctx):
    with patch.object(settings, "demo_mode", True):
        res = await zigbee_join_capture(ZigbeeJoinCaptureInput(channel=_DEMO_CHANNEL), mock_ctx)
    assert res.key_captured is True
    assert res.network_key == _DEMO_NETWORK_KEY


async def test_zigbee_key_inject_demo_joins(mock_ctx):
    with patch.object(settings, "demo_mode", True):
        res = await zigbee_key_inject(
            ZigbeeKeyInjectInput(channel=_DEMO_CHANNEL, network_key=_DEMO_NETWORK_KEY), mock_ctx
        )
    assert res.joined is True  # activates zigbee_key_injected


async def test_zigbee_real_mode_reports_hardware_requirement(mock_ctx):
    with patch.object(settings, "demo_mode", False):
        res = await zigbee_scan(ZigbeeScanInput(), mock_ctx)
    assert res.networks_found == 0
    assert res.error and "coordinator" in res.error.lower()


# ── Outbound trio (demo fixtures) ────────────────────────────────────────────


async def test_outbound_analyzer_demo_flags_http(mock_ctx):
    with patch.object(settings, "demo_mode", True):
        res = await outbound_analyzer(OutboundAnalyzerInput(target="10.0.0.5"), mock_ctx)
    assert res.http_destinations  # non-empty → activates outbound_http


async def test_dns_hijack_simulator_demo(mock_ctx):
    with patch.object(settings, "demo_mode", True):
        res = await dns_hijack_simulator(DnsHijackSimulatorInput(target="10.0.0.5"), mock_ctx)
    assert res.followed_redirect is True


async def test_tls_validation_tester_demo(mock_ctx):
    with patch.object(settings, "demo_mode", True):
        res = await tls_validation_tester(TlsValidationTesterInput(target="10.0.0.5"), mock_ctx)
    assert res.accepts_invalid_cert is True


async def test_outbound_real_mode_requires_gateway(mock_ctx):
    with patch.object(settings, "demo_mode", False):
        res = await outbound_analyzer(OutboundAnalyzerInput(target="10.0.0.5"), mock_ctx)
    assert res.http_destinations == []
    assert res.error and "gateway" in res.error.lower()


# ── debug_interface_scan (demo fixture) ──────────────────────────────────────


async def test_debug_interface_scan_demo_console_open(mock_ctx):
    with patch.object(settings, "demo_mode", True):
        res = await debug_interface_scan(DebugInterfaceScanInput(), mock_ctx)
    assert res.console_accessible is True
    assert res.banner and "U-Boot" in res.banner


async def test_debug_interface_scan_real_requires_port(mock_ctx):
    with patch.object(settings, "demo_mode", False):
        res = await debug_interface_scan(DebugInterfaceScanInput(), mock_ctx)
    assert res.console_accessible is False
    assert res.error and "port is required" in res.error


# ── cve_lookup (real, bundled DB) ────────────────────────────────────────────


async def test_cve_lookup_matches_known_version(mock_ctx):
    res = await cve_lookup(CveLookupInput(service="dropbear", version="Dropbear 2016.74"), mock_ctx)
    assert res.match_count >= 1
    assert any(c.id == "CVE-2016-7406" for c in res.matched_cves)


async def test_cve_lookup_no_match_for_unknown(mock_ctx):
    res = await cve_lookup(CveLookupInput(service="nonexistent-svc", version="9.9"), mock_ctx)
    assert res.match_count == 0
    assert res.matched_cves == []


# ── unsigned_update_check (real, static analysis) ────────────────────────────


async def test_unsigned_update_check_flags_http_no_sig(mock_ctx, tmp_path):
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "ota.conf").write_text(
        "UPDATE_URL=http://updates.example.com/firmware/latest.bin\n"
    )
    res = await unsigned_update_check(
        UnsignedUpdateCheckInput(extract_dir=str(tmp_path)), mock_ctx
    )
    assert res.unsigned_http_update is True
    assert res.http_update_urls


async def test_unsigned_update_check_clears_when_signature_present(mock_ctx, tmp_path):
    (tmp_path / "ota.conf").write_text("UPDATE_URL=http://updates.example.com/fw.bin\n")
    (tmp_path / "update.sig").write_text("signature-bytes")
    res = await unsigned_update_check(
        UnsignedUpdateCheckInput(extract_dir=str(tmp_path)), mock_ctx
    )
    assert res.unsigned_http_update is False
    assert res.signature_indicators


async def test_unsigned_update_check_missing_dir(mock_ctx):
    res = await unsigned_update_check(
        UnsignedUpdateCheckInput(extract_dir="/no/such/dir/here"), mock_ctx
    )
    assert res.unsigned_http_update is False
    assert res.error


# ── command_injection_tester (marker logic + graceful failure) ───────────────


def test_find_marker_detects_command_output():
    assert _find_marker("PING ... uid=0(root) gid=0(root)") == "uid=0(root)"
    assert _find_marker("root:x:0:0:root:/root:/bin/sh") is not None
    assert _find_marker("normal page, nothing here") is None


async def test_command_injection_tester_handles_dead_host(mock_ctx):
    # Unreachable host → no exception, injected False, attempts counted.
    res = await command_injection_tester(
        CommandInjectionTesterInput(
            base_url="http://127.0.0.1:1", paths=["/x"], param_fields=["q"]
        ),
        mock_ctx,
    )
    assert res.injected is False
    assert res.attempts > 0


# ── End-to-end: every terminal chain goes active ─────────────────────────────


def _port_scan_open():
    return {
        "target": "192.168.1.50", "host_up": True, "open_port_count": 4,
        "ports": [
            {"port": 23, "protocol": "tcp", "state": "open", "service": "telnet", "version": None},
            {"port": 22, "protocol": "tcp", "state": "open", "service": "dropbear",
             "version": "Dropbear sshd 2016.74"},
            {"port": 80, "protocol": "tcp", "state": "open", "service": "http", "version": None},
            {"port": 5683, "protocol": "udp", "state": "open", "service": "coap", "version": None},
        ],
        "elapsed_seconds": 1.0, "timed_out": False, "error": None,
    }


def _port_scan_locked():
    return {
        "target": "192.168.1.50", "host_up": True, "open_port_count": 0,
        "ports": [], "elapsed_seconds": 1.0, "timed_out": False, "error": None,
    }


def _full_vulnerable_session() -> dict:
    return {
        "port_scan": [_port_scan_open(), _port_scan_locked()],
        "telnet_brute": {"valid_credentials": [["root", "root"]], "error": None},
        "mqtt_probe": {"anonymous_access": True, "error": None},
        "mqtt_topic_enum": {"topics_found": ["device/cmd"], "error": None},
        "mqtt_publish": {"accepted": True, "error": None},
        "extract_firmware": {
            "extracted": True, "signature_count": 2,
            "signatures": [
                {"offset": 0, "description": "U-Boot bootloader", "name": None},
                {"offset": 64, "description": "squashfs filesystem", "name": None},
            ],
            "error": None,
        },
        "unsigned_update_check": {"unsigned_http_update": True, "error": None},
        "scan_hardcoded_creds": {"hit_count": 2, "hits": [{"file": "etc/passwd"}], "error": None},
        "ssh_brute": {"valid_credentials": [["root", "admin"]], "error": None},
        "fuzz_web_paths": {
            "hits": [{"path": "/cgi-bin/admin", "url": "http://x/cgi-bin/admin",
                      "status_code": 200, "content_length": None, "content_type": None}],
            "error": None,
        },
        "test_default_creds_http": {"successful_credentials": [["admin", "admin"]], "error": None},
        "command_injection_tester": {"injected": True, "error": None},
        "ble_scan": {"device_count": 1, "devices": [], "scan_seconds": 10, "error": None},
        "gatt_enum": {"writable_no_auth": [{"uuid": "fe25", "handle": 18, "properties": ["write"]}],
                      "error": None},
        "gatt_write": {"accepted": True, "error": None},
        "coap_discover": {
            "resources": [{"path": "/actuator/lock", "attributes": {"if": "actuator"}}],
            "error": None,
        },
        "coap_put": {"accepted": True, "error": None},
        "outbound_analyzer": {"http_destinations": ["api.iot-cloud.example"], "error": None},
        "dns_hijack_simulator": {"followed_redirect": True, "error": None},
        "tls_validation_tester": {"accepts_invalid_cert": True, "error": None},
        "zigbee_scan": {"networks_found": 1, "error": None},
        "zigbee_join_capture": {"key_captured": True, "error": None},
        "zigbee_key_inject": {"joined": True, "error": None},
        "cve_lookup": {"matched_cves": [{"id": "CVE-2016-7406", "cvss": "", "summary": ""}],
                       "error": None},
        "debug_interface_scan": {"console_accessible": True, "error": None},
    }


# Every root's terminal (leaf-with-outcome) node id.
_TERMINAL_CHAIN_IDS = {
    "telnet_default_cred",
    "mqtt_publish_accepted",
    "firmware_unsigned_http_update",
    "firmware_ssh_cred_works",
    "web_command_injection",
    "ble_write_accepted",
    "coap_put_accepted",
    "tls_accepts_selfsigned",
    "zigbee_key_injected",
    "component_known_cve",
    "debug_interface_exposed",
}


async def test_all_chains_go_active_with_full_session(mock_ctx):
    res = await walk_vuln_tree(
        WalkVulnTreeInput(session_outputs=_full_vulnerable_session(), target="192.168.1.50"),
        mock_ctx,
    )
    assert res.error is None
    active_ids = {c.chain_id for c in res.active_chains}
    missing = _TERMINAL_CHAIN_IDS - active_ids
    assert not missing, f"chains failed to activate: {missing}"
    # Fully-confirmed chains must not also linger as partial leads.
    partial_ids = {p.chain_id for p in res.partial_chains}
    assert not (_TERMINAL_CHAIN_IDS & partial_ids)


def test_terminal_set_matches_tree_roots():
    """Guard: the expected terminal set has exactly one entry per tree root."""
    tree = _load_tree()
    assert len(tree["roots"]) == len(_TERMINAL_CHAIN_IDS)
