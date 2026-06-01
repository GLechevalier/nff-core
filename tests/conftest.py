"""Shared fixtures for unit and integration tests."""
from __future__ import annotations

import secrets
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


@pytest.fixture
def sample_artifact_bytes() -> bytes:
    """1 KB of deterministic pseudo-random firmware bytes."""
    rng = secrets.SystemRandom()
    return bytes(rng.randint(0, 255) for _ in range(1024))


@pytest.fixture
def ed25519_keypair() -> tuple[str, str]:
    """Returns (private_pem, public_pem) for a fresh Ed25519 key."""
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return priv_pem, pub_pem


def _make_supabase_mock() -> MagicMock:
    """Build a MagicMock that mimics the Supabase async client interface.

    The pattern used by supabase-py is:
        client.table("name").select("*").execute()  → returns APIResponse
        client.storage.from_("bucket").upload(...)
    """
    mock = MagicMock()

    # --- table API ---
    table_mock = MagicMock()
    # Each chained call returns the same mock so .select().eq().execute() works.
    table_mock.select.return_value = table_mock
    table_mock.insert.return_value = table_mock
    table_mock.update.return_value = table_mock
    table_mock.delete.return_value = table_mock
    table_mock.eq.return_value = table_mock
    table_mock.in_.return_value = table_mock
    table_mock.neq.return_value = table_mock
    table_mock.order.return_value = table_mock
    table_mock.limit.return_value = table_mock
    table_mock.execute = MagicMock(return_value=MagicMock(data=[], error=None))
    mock.table.return_value = table_mock

    # --- storage API ---
    storage_bucket = MagicMock()
    storage_bucket.upload.return_value = MagicMock(error=None)
    storage_bucket.download.return_value = b""
    storage_bucket.remove.return_value = MagicMock(error=None)
    mock.storage.from_.return_value = storage_bucket

    return mock


@pytest.fixture
def mock_supabase():
    """Inject a mock Supabase client by setting nff_ota.client._client directly.

    All ops modules call get_client() which reads the module-level _client.
    Setting _client bypasses the RuntimeError guard and works regardless of
    how the function was imported.
    """
    import nff_ota.client as _client_module

    mock = _make_supabase_mock()
    original = _client_module._client
    _client_module._client = mock
    yield mock
    _client_module._client = original


def make_table_response(data: list[dict]) -> MagicMock:
    """Helper to set the return value of .execute() on a table mock."""
    response = MagicMock()
    response.data = data
    response.error = None
    return response
