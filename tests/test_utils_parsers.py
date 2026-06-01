"""Unit tests for nff_pentester.utils.parsers."""

import pytest

from tests.conftest import BINWALK_JSON_LIST, NMAP_XML_DOWN_HOST, NMAP_XML_ONE_HOST

from nff_pentester.utils.parsers import parse_binwalk_json, parse_nmap_xml


# ── parse_nmap_xml ─────────────────────────────────────────────────────────────

def test_empty_input_returns_empty_hosts():
    result = parse_nmap_xml("")
    assert result["hosts"] == []


def test_blank_whitespace_returns_empty_hosts():
    result = parse_nmap_xml("   \n  ")
    assert result["hosts"] == []


def test_single_host_ip_extracted():
    result = parse_nmap_xml(NMAP_XML_ONE_HOST)
    assert len(result["hosts"]) == 1
    assert result["hosts"][0]["ip"] == "192.168.1.1"


def test_single_host_mac_and_vendor_extracted():
    result = parse_nmap_xml(NMAP_XML_ONE_HOST)
    host = result["hosts"][0]
    assert host["mac"] == "AA:BB:CC:DD:EE:FF"
    assert host["vendor"] == "TP-Link"


def test_single_host_hostname_extracted():
    result = parse_nmap_xml(NMAP_XML_ONE_HOST)
    assert result["hosts"][0]["hostname"] == "router.local"


def test_single_host_port_details_extracted():
    result = parse_nmap_xml(NMAP_XML_ONE_HOST)
    ports = result["hosts"][0]["ports"]
    assert len(ports) == 1
    p = ports[0]
    assert p["port"] == 80
    assert p["protocol"] == "tcp"
    assert p["state"] == "open"
    assert p["service"] == "http"
    assert "nginx" in p["version"]


def test_elapsed_time_parsed():
    result = parse_nmap_xml(NMAP_XML_ONE_HOST)
    assert result["elapsed"] == pytest.approx(1.23)


def test_down_host_excluded():
    result = parse_nmap_xml(NMAP_XML_DOWN_HOST)
    assert result["hosts"] == []


def test_invalid_xml_returns_empty_gracefully():
    result = parse_nmap_xml("<not valid xml >>>")
    assert result["hosts"] == []
    assert result["elapsed"] == 0.0


# ── parse_binwalk_json ─────────────────────────────────────────────────────────

def test_binwalk_empty_string_returns_empty_list():
    assert parse_binwalk_json("") == []


def test_binwalk_whitespace_returns_empty_list():
    assert parse_binwalk_json("  ") == []


def test_binwalk_valid_list_format():
    result = parse_binwalk_json(BINWALK_JSON_LIST)
    assert len(result) == 1
    assert result[0]["offset"] == 0
    assert result[0]["description"] == "gzip compressed data"


def test_binwalk_dict_format_returns_values_as_list():
    result = parse_binwalk_json('{"0": {"offset": 0, "description": "squashfs"}}')
    assert len(result) == 1
    assert result[0]["offset"] == 0


def test_binwalk_invalid_json_returns_empty_list():
    assert parse_binwalk_json("{bad json!!!") == []
