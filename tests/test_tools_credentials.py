"""Unit tests for credential tools: lookup_default_creds, ssh_brute, telnet_brute."""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from nff_pentester.tools.credentials.brute import (
    SshBruteInput,
    TelnetBruteInput,
    ssh_brute,
    telnet_brute,
)
from nff_pentester.tools.credentials.database import (
    LookupDefaultCredsInput,
    lookup_default_creds,
)


# ── lookup_default_creds ───────────────────────────────────────────────────────

async def test_lookup_match_hikvision(mock_ctx):
    params = LookupDefaultCredsInput(vendor="Hikvision")
    result = await lookup_default_creds(params, mock_ctx)
    assert result.match_count >= 2
    for cred in result.credentials:
        assert cred.vendor == "Hikvision"


async def test_lookup_no_match(mock_ctx):
    params = LookupDefaultCredsInput(vendor="xyzzy_nonexistent_brand_1234")
    result = await lookup_default_creds(params, mock_ctx)
    assert result.match_count == 0
    assert result.credentials == []


async def test_lookup_case_insensitive(mock_ctx):
    params = LookupDefaultCredsInput(vendor="hikvision")
    result = await lookup_default_creds(params, mock_ctx)
    assert result.match_count >= 1


async def test_lookup_device_type_filter(mock_ctx):
    params = LookupDefaultCredsInput(vendor="Hikvision", device_type="ip-camera")
    result = await lookup_default_creds(params, mock_ctx)
    assert result.match_count >= 1
    for cred in result.credentials:
        assert cred.device_type == "ip-camera"


async def test_lookup_vendor_query_preserved(mock_ctx):
    params = LookupDefaultCredsInput(vendor="TP-Link")
    result = await lookup_default_creds(params, mock_ctx)
    assert result.vendor_query == "TP-Link"


async def test_lookup_returns_username_and_password(mock_ctx):
    params = LookupDefaultCredsInput(vendor="Generic")
    result = await lookup_default_creds(params, mock_ctx)
    assert result.match_count >= 1
    cred = result.credentials[0]
    assert isinstance(cred.username, str)
    assert isinstance(cred.password, str)


# ── ssh_brute ──────────────────────────────────────────────────────────────────

async def test_ssh_brute_no_asyncssh(mock_ctx):
    with patch.dict(sys.modules, {"asyncssh": None}):
        params = SshBruteInput(host="127.0.0.1", credentials=[("admin", "admin")])
        result = await ssh_brute(params, mock_ctx)
    assert result.error is not None
    assert "asyncssh" in result.error
    assert result.valid_credentials == []


async def test_ssh_brute_finds_valid_creds(mock_ctx):
    mock_conn = AsyncMock()
    mock_conn_cm = AsyncMock()
    mock_conn_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn_cm.__aexit__ = AsyncMock(return_value=False)

    mock_asyncssh = MagicMock()
    mock_asyncssh.connect = MagicMock(return_value=mock_conn_cm)
    mock_asyncssh.PermissionDenied = PermissionError

    with patch.dict(sys.modules, {"asyncssh": mock_asyncssh}):
        params = SshBruteInput(host="127.0.0.1", credentials=[("admin", "admin")])
        result = await ssh_brute(params, mock_ctx)

    assert ("admin", "admin") in result.valid_credentials
    assert result.error is None


async def test_ssh_brute_all_denied(mock_ctx):
    class MockPermDenied(Exception):
        pass

    mock_error_cm = AsyncMock()
    mock_error_cm.__aenter__ = AsyncMock(side_effect=MockPermDenied("denied"))
    mock_error_cm.__aexit__ = AsyncMock(return_value=False)

    mock_asyncssh = MagicMock()
    mock_asyncssh.connect = MagicMock(return_value=mock_error_cm)
    mock_asyncssh.PermissionDenied = MockPermDenied

    with patch.dict(sys.modules, {"asyncssh": mock_asyncssh}):
        params = SshBruteInput(
            host="127.0.0.1",
            credentials=[("admin", "wrong1"), ("root", "wrong2")],
        )
        result = await ssh_brute(params, mock_ctx)

    assert result.valid_credentials == []
    assert result.attempts == 2
    assert result.error is None


async def test_ssh_brute_cap_applied(mock_ctx):
    class MockPermDenied(Exception):
        pass

    mock_error_cm = AsyncMock()
    mock_error_cm.__aenter__ = AsyncMock(side_effect=MockPermDenied("denied"))
    mock_error_cm.__aexit__ = AsyncMock(return_value=False)

    mock_asyncssh = MagicMock()
    mock_asyncssh.connect = MagicMock(return_value=mock_error_cm)
    mock_asyncssh.PermissionDenied = MockPermDenied

    from nff_pentester.config import settings

    with patch.dict(sys.modules, {"asyncssh": mock_asyncssh}):
        with patch.object(settings, "max_brute_attempts", 2):
            creds = [("u", f"p{i}") for i in range(5)]
            params = SshBruteInput(host="127.0.0.1", credentials=creds)
            result = await ssh_brute(params, mock_ctx)

    assert result.capped is True
    assert result.attempts == 2


# ── telnet_brute ───────────────────────────────────────────────────────────────

async def test_telnet_brute_connection_refused(mock_ctx):
    with patch("asyncio.open_connection", new=AsyncMock(side_effect=ConnectionRefusedError("refused"))):
        params = TelnetBruteInput(
            host="192.168.1.1",
            credentials=[("admin", "admin")],
        )
        result = await telnet_brute(params, mock_ctx)
    assert all(not a.success for a in result.details)
    assert result.valid_credentials == []
    assert result.error is None


async def test_telnet_brute_success(mock_ctx):
    mock_reader = AsyncMock()
    mock_writer = MagicMock()
    mock_writer.close = MagicMock()
    mock_reader.read = AsyncMock(side_effect=[b"login: ", b"Password: ", b"$ welcome"])

    with patch("asyncio.open_connection", new=AsyncMock(return_value=(mock_reader, mock_writer))):
        params = TelnetBruteInput(
            host="192.168.1.1",
            credentials=[("admin", "admin")],
            login_prompt="login:",
            password_prompt="Password:",
            success_prompt="$",
        )
        result = await telnet_brute(params, mock_ctx)

    assert ("admin", "admin") in result.valid_credentials


async def test_telnet_brute_wrong_password(mock_ctx):
    mock_reader = AsyncMock()
    mock_writer = MagicMock()
    mock_writer.close = MagicMock()
    mock_reader.read = AsyncMock(side_effect=[b"login: ", b"Password: ", b"Login incorrect"])

    with patch("asyncio.open_connection", new=AsyncMock(return_value=(mock_reader, mock_writer))):
        params = TelnetBruteInput(
            host="192.168.1.1",
            credentials=[("admin", "wrongpass")],
            login_prompt="login:",
            password_prompt="Password:",
            success_prompt="$",
        )
        result = await telnet_brute(params, mock_ctx)

    assert result.valid_credentials == []
    assert result.details[0].success is False


async def test_telnet_brute_cap_applied(mock_ctx):
    from nff_pentester.config import settings

    with patch("asyncio.open_connection", new=AsyncMock(side_effect=ConnectionRefusedError())):
        with patch.object(settings, "max_brute_attempts", 2):
            creds = [("u", f"p{i}") for i in range(5)]
            params = TelnetBruteInput(host="192.168.1.1", credentials=creds)
            result = await telnet_brute(params, mock_ctx)

    assert result.capped is True
    assert result.attempts == 2
