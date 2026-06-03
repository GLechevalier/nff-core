"""Unit tests for nff_fleet.signing — ECDSA-P256 command signing.

These tests verify that _build_tbs() produces the exact string format that
nff_security.c on the device expects, and that sign()/verify() are consistent.
All tests use an ephemeral in-memory key — no disk access required.
"""
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

import nff_fleet.signing as signing


@pytest.fixture(autouse=True)
def ephemeral_key(monkeypatch):
    """Inject a fresh P-256 key into the signing module before each test."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()
    monkeypatch.setattr(signing, "_private_key", private_key)
    monkeypatch.setattr(signing, "_public_key", public_key)


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
    tbs = signing._build_tbs(payload)
    assert tbs == f"ota|2.0.0|{'aa' * 32}|deadbeef|1700000000"


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
    signed = signing.sign(payload)
    assert "cmd_sig" in signed
    assert signing.verify(signed) is True


def test_sign_verify_roundtrip_ping():
    payload = {"action": "ping", "nonce": "12345678", "timestamp": 1000}
    signed = signing.sign(payload)
    assert signing.verify(signed) is True


# ---------------------------------------------------------------------------
# Rejection tests
# ---------------------------------------------------------------------------

def test_tampered_sig_rejected():
    payload = {"action": "ping", "nonce": "aabbccdd", "timestamp": 1}
    signed = signing.sign(payload)
    # Flip last two hex chars of the DER signature
    bad_sig = signed["cmd_sig"][:-2] + ("00" if signed["cmd_sig"][-2:] != "00" else "ff")
    assert signing.verify({**signed, "cmd_sig": bad_sig}) is False


def test_missing_sig_rejected():
    payload = {"action": "ping", "nonce": "aabbccdd", "timestamp": 1}
    assert signing.verify(payload) is False


def test_unloaded_key_raises(monkeypatch):
    monkeypatch.setattr(signing, "_private_key", None)
    monkeypatch.setattr(signing, "_public_key", None)
    with pytest.raises(RuntimeError, match="Signing key not loaded"):
        signing.sign({"action": "ping", "nonce": "00000000", "timestamp": 0})
