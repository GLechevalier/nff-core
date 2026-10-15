"""Smoke tests: verify the server and all tool modules import without errors."""

import pytest
from mcp.server.fastmcp import FastMCP


def test_server_imports() -> None:
    """Server module imports and the root FastMCP instance is constructed."""
    from nff_pentester.server import mcp
    assert isinstance(mcp, FastMCP)


def test_tools_registered() -> None:
    """All expected tool names are registered on the root server."""
    from nff_pentester.server import mcp
    import asyncio
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}

    expected = {
        "port_scan", "discover_devices",
        "mqtt_probe", "mqtt_topic_enum", "mqtt_publish",
        "coap_discover", "coap_put",
        "ble_scan", "gatt_enum", "gatt_write",
        "zigbee_scan", "zigbee_join_capture", "zigbee_key_inject",
        "extract_firmware", "analyze_strings", "scan_hardcoded_creds", "unsigned_update_check",
        "fuzz_web_paths", "test_default_creds_http", "command_injection_tester",
        "lookup_default_creds", "ssh_brute", "telnet_brute",
        "walk_vuln_tree", "sandbox_status",
        "cve_lookup",
        "outbound_analyzer", "dns_hijack_simulator", "tls_validation_tester",
        "debug_interface_scan",
    }
    assert expected <= names, f"Missing tools: {expected - names}"


def test_resources_registered() -> None:
    """All resource URIs are registered."""
    from nff_pentester.server import mcp
    import asyncio
    resources = asyncio.run(mcp.list_resources())
    uris = {str(r.uri) for r in resources}
    assert "pentester://wordlists/common-passwords" in uris
    assert "pentester://fingerprints/default-creds" in uris


def test_prompts_registered() -> None:
    """All workflow prompts are registered."""
    from nff_pentester.server import mcp
    import asyncio
    prompts = asyncio.run(mcp.list_prompts())
    names = {p.name for p in prompts}
    assert "full_iot_assessment" in names
    assert "mqtt_broker_audit" in names
    assert "firmware_review" in names


def test_config_defaults() -> None:
    """The code defaults are correct, independent of any local .env / .env.dev."""
    from nff_pentester.config import PentesterSettings
    # _env_file=None ignores a developer's local dotenv (e.g. the demo preset that
    # overrides timeouts) so this asserts the in-code defaults, not the dev's env.
    s = PentesterSettings(_env_file=None)
    assert s.tool_timeout_seconds == 120
    assert s.max_brute_attempts == 200
    assert s.mqtt_port == 1883


def test_http_transport_defaults() -> None:
    """Server defaults to streamable-HTTP on loopback:3006 like the siblings."""
    from nff_pentester.config import PentesterSettings

    # Code defaults, independent of any local dotenv override.
    s = PentesterSettings(_env_file=None)
    assert s.mcp_transport == "streamable-http"
    assert s.mcp_host == "127.0.0.1"
    assert s.mcp_port == 3006


def test_run_command_not_found() -> None:
    """run_command handles a missing binary gracefully."""
    import asyncio
    from nff_pentester.utils.subprocess import run_command
    result = asyncio.run(run_command(["__nonexistent_binary__"]))
    assert result.returncode != 0
    assert not result.timed_out


def test_parse_nmap_xml_empty() -> None:
    """parse_nmap_xml returns empty hosts on blank input."""
    from nff_pentester.utils.parsers import parse_nmap_xml
    result = parse_nmap_xml("")
    assert result["hosts"] == []


def test_normalise_target_rejects_injection() -> None:
    """normalise_target rejects shell-injection characters."""
    from nff_pentester.utils.net import normalise_target
    with pytest.raises(ValueError):
        normalise_target("192.168.1.1; rm -rf /")
