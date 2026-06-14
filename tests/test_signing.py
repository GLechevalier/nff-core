"""Unit tests for nff_fleet.signing — per-project ECDSA-P256 command signing.

These tests verify that _build_tbs() produces the exact string format that nff_security.c on
the device expects, that sign()/verify() round-trip per project, and that a command signed for
one project does NOT verify against another project's key (DEVICE_OWNERSHIP_DESIGN.md §6).
"""
import pytest

import nff_fleet.signing as signing

PROJ_A = "project-a"
PROJ_B = "project-b"


@pytest.fixture(autouse=True)
def project_keys(tmp_path, monkeypatch):
    """Point the signing module at a temp key dir and create two project keys."""
    monkeypatch.setattr(signing.settings, "data_dir", tmp_path)
    signing.clear_cache()
    signing.ensure_project_key(PROJ_A)
    signing.ensure_project_key(PROJ_B)
    yield
    signing.clear_cache()


# ---------------------------------------------------------------------------
# TBS format tests — must exactly match nff_security.c build_tbs()
# ---------------------------------------------------------------------------

def test_tbs_ota():
    payload = {
        "action": "ota",
        "version": "2.0.0",
        "sha256": "aa" * 32,
        "nonce": "deadbeef",
        "timestamp": 1700000000,
    }
    assert signing._build_tbs(payload) == f"ota|2.0.0|{'aa' * 32}|deadbeef|1700000000"


def test_tbs_ping():
    payload = {"action": "ping", "nonce": "cafebabe", "timestamp": 42}
    assert signing._build_tbs(payload) == "ping|cafebabe|42"


def test_tbs_reboot():
    payload = {"action": "reboot", "nonce": "11223344", "timestamp": 999}
    assert signing._build_tbs(payload) == "reboot|11223344|999"


# ---------------------------------------------------------------------------
# sign() / verify() round-trip tests
# ---------------------------------------------------------------------------

def test_sign_verify_roundtrip_ota():
    payload = {
        "action": "ota",
        "version": "2.0.0",
        "sha256": "bb" * 32,
        "nonce": "aabbccdd",
        "timestamp": 1700000001,
    }
    signed = signing.sign(payload, PROJ_A)
    assert "cmd_sig" in signed
    assert signing.verify(signed, PROJ_A) is True


def test_sign_verify_roundtrip_ping():
    payload = {"action": "ping", "nonce": "12345678", "timestamp": 1000}
    signed = signing.sign(payload, PROJ_A)
    assert signing.verify(signed, PROJ_A) is True


# ---------------------------------------------------------------------------
# Cross-project isolation (the core §6 guarantee)
# ---------------------------------------------------------------------------

def test_command_for_one_project_fails_on_another():
    payload = {"action": "ping", "nonce": "12345678", "timestamp": 1000}
    signed = signing.sign(payload, PROJ_A)
    assert signing.verify(signed, PROJ_A) is True
    assert signing.verify(signed, PROJ_B) is False


def test_uncompressed_key_is_65_bytes_and_distinct():
    key_a = signing.public_key_uncompressed(PROJ_A)
    key_b = signing.public_key_uncompressed(PROJ_B)
    assert len(key_a) == 65 and key_a[0] == 0x04
    assert key_a != key_b


# ---------------------------------------------------------------------------
# Rejection / error tests
# ---------------------------------------------------------------------------

def test_tampered_sig_rejected():
    payload = {"action": "ping", "nonce": "aabbccdd", "timestamp": 1}
    signed = signing.sign(payload, PROJ_A)
    bad_sig = signed["cmd_sig"][:-2] + ("00" if signed["cmd_sig"][-2:] != "00" else "ff")
    assert signing.verify({**signed, "cmd_sig": bad_sig}, PROJ_A) is False


def test_missing_sig_rejected():
    payload = {"action": "ping", "nonce": "aabbccdd", "timestamp": 1}
    assert signing.verify(payload, PROJ_A) is False


def test_unknown_project_raises():
    with pytest.raises(RuntimeError, match="No command-signing key"):
        signing.sign({"action": "ping", "nonce": "00000000", "timestamp": 0}, "no-such-project")


def test_ensure_project_key_is_idempotent():
    path1 = signing.ensure_project_key(PROJ_A)
    contents = path1.read_bytes()
    path2 = signing.ensure_project_key(PROJ_A)
    assert path1 == path2
    assert path2.read_bytes() == contents  # not regenerated
