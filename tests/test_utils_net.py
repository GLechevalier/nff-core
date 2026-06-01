"""Unit tests for nff_pentester.utils.net."""

import pytest

from nff_pentester.utils.net import (
    expand_cidr,
    is_valid_ip,
    normalise_target,
    port_list_to_str,
)


def test_valid_ip_passes():
    assert normalise_target("192.168.1.1") == "192.168.1.1"


def test_valid_cidr_passes():
    assert normalise_target("192.168.1.0/24") == "192.168.1.0/24"


def test_valid_hostname_passes():
    assert normalise_target("router.local") == "router.local"


def test_strips_leading_trailing_whitespace():
    assert normalise_target("  192.168.1.1  ") == "192.168.1.1"


def test_rejects_semicolon():
    with pytest.raises(ValueError):
        normalise_target("192.168.1.1; rm -rf /")


def test_rejects_ampersand():
    with pytest.raises(ValueError):
        normalise_target("192.168.1.1 && id")


def test_rejects_pipe():
    with pytest.raises(ValueError):
        normalise_target("192.168.1.1 | cat /etc/passwd")


def test_rejects_dollar():
    with pytest.raises(ValueError):
        normalise_target("$HOME")


def test_rejects_backtick():
    with pytest.raises(ValueError):
        normalise_target("`id`")


def test_rejects_newline():
    with pytest.raises(ValueError):
        normalise_target("192.168.1.1\nwhoami")


def test_expand_cidr_slash30_returns_two_hosts():
    hosts = expand_cidr("192.168.1.0/30")
    assert hosts == ["192.168.1.1", "192.168.1.2"]


def test_expand_cidr_excludes_network_and_broadcast():
    hosts = expand_cidr("10.0.0.0/30")
    assert "10.0.0.0" not in hosts
    assert "10.0.0.3" not in hosts
    assert len(hosts) == 2


def test_is_valid_ip_returns_true_for_valid_address():
    assert is_valid_ip("10.0.0.1") is True


def test_is_valid_ip_returns_false_for_invalid_address():
    assert is_valid_ip("999.x.y.z") is False


def test_port_list_to_str_sorts_and_joins():
    assert port_list_to_str([443, 22, 80]) == "22,80,443"


def test_port_list_to_str_single_port():
    assert port_list_to_str([22]) == "22"
