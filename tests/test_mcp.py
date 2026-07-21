"""Tests for nff MCP server.

Unit tests call handler functions directly (fast, no transport overhead).
HTTP integration tests spin up a real uvicorn server on a free port and
exercise the full MCP-over-HTTP protocol via mcp.ClientSession.
"""

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from nff.mcp_server import (
    _make_starlette_app,
    _resolve_fqbn_and_port,
    _resolve_port,
    authenticate,
    auth_clear,
    auth_reconnect,
    complete_authentication,
    flash,
    get_device_info,
    list_devices,
    reset_device,
    serial_read,
    serial_write,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_TOKEN = "test_access"
_TEST_REFRESH = "test_refresh"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def _authed_mcp_client(mcp_url: str, token: str = _TEST_TOKEN):
    """streamable_http_client pre-configured with a Bearer token header."""
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(mcp_url, http_client=http) as streams:
            yield streams


# ---------------------------------------------------------------------------
# Fixture: live HTTP MCP server on a free port
# ---------------------------------------------------------------------------

@pytest.fixture()
async def mcp_url(isolated_config):
    """Start a real uvicorn/Starlette MCP HTTP server; yield its /mcp URL.

    Uses isolated_config so tests never touch ~/.nff/config.json, and the
    Bearer auth check reads from the temp config (no token by default).
    """
    port = _free_port()
    starlette_app = _make_starlette_app()
    cfg = uvicorn.Config(starlette_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(cfg)

    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5.0)


@pytest.fixture()
def base_url(mcp_url: str) -> str:
    """Base server URL (without /mcp) for testing OAuth endpoints."""
    return mcp_url[: -len("/mcp")]


@pytest.fixture(autouse=True)
def _gate_off_by_default(monkeypatch):
    """nff ships ungated. Clear any stray NFF_MCP_REQUIRE_AUTH so the suite's default
    matches shipped behavior (gate OFF); tests that need the gate set it explicitly."""
    monkeypatch.delenv("NFF_MCP_REQUIRE_AUTH", raising=False)


# ===========================================================================
# UNIT TESTS — resolver helpers (no transport)
# ===========================================================================

def test_resolve_fqbn_and_port_returns_explicit_args(isolated_config):
    fqbn, port = _resolve_fqbn_and_port("arduino:avr:uno", "COM3")
    assert fqbn == "arduino:avr:uno"
    assert port == "COM3"


def test_resolve_fqbn_and_port_falls_back_to_config(isolated_config):
    from nff import config as cfg
    cfg.set_default_device(port="COM10", board="ESP32 (CP210x)",
                           fqbn="esp32:esp32:esp32", baud=115200)
    fqbn, port = _resolve_fqbn_and_port(None, None)
    assert fqbn == "esp32:esp32:esp32"
    assert port == "COM10"


def test_resolve_fqbn_and_port_explicit_overrides_config(isolated_config):
    from nff import config as cfg
    cfg.set_default_device(port="COM10", board="ESP32 (CP210x)",
                           fqbn="esp32:esp32:esp32", baud=115200)
    fqbn, port = _resolve_fqbn_and_port("arduino:avr:uno", "COM3")
    assert fqbn == "arduino:avr:uno"
    assert port == "COM3"


def test_resolve_fqbn_and_port_raises_when_both_missing(isolated_config):
    with pytest.raises(ValueError, match="Missing"):
        _resolve_fqbn_and_port(None, None)


def test_resolve_fqbn_and_port_raises_when_port_missing(isolated_config):
    from nff import config as cfg
    cfg.set_default_device(port=None, board="X", fqbn="x:x:x")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Missing"):
        _resolve_fqbn_and_port("arduino:avr:uno", None)


def test_mcp_resolve_port_returns_explicit():
    assert _resolve_port("COM5") == "COM5"


def test_mcp_resolve_port_falls_back_to_config(isolated_config):
    from nff import config as cfg
    cfg.set_default_device(port="COM10", board="ESP32 (CP210x)",
                           fqbn="esp32:esp32:esp32", baud=115200)
    assert _resolve_port(None) == "COM10"


def test_mcp_resolve_port_raises_when_nothing_set(isolated_config):
    with pytest.raises(ValueError, match="No port"):
        _resolve_port(None)


# ===========================================================================
# UNIT TESTS — async handler functions (no transport)
# ===========================================================================

async def test_list_devices_returns_device_list():
    from nff.tools.boards import DetectedDevice
    mock_device = DetectedDevice(
        port="COM10", board="ESP32 (CP210x)",
        fqbn="esp32:esp32:esp32", vendor_id="10c4", product_id="ea60",
    )
    with patch("nff.mcp_server.boards_module.list_devices", return_value=[mock_device]):
        result = await list_devices()

    assert len(result["devices"]) == 1
    d = result["devices"][0]
    assert d["port"] == "COM10"
    assert d["board"] == "ESP32 (CP210x)"
    assert d["fqbn"] == "esp32:esp32:esp32"
    assert d["vendor_id"] == "10c4"
    assert d["product_id"] == "ea60"


async def test_list_devices_returns_empty_list():
    with patch("nff.mcp_server.boards_module.list_devices", return_value=[]):
        result = await list_devices()
    assert result["devices"] == []


async def test_flash_returns_ok_on_success(isolated_config):
    from nff import config as cfg
    cfg.set_default_device(port="COM10", board="ESP32 (CP210x)",
                           fqbn="esp32:esp32:esp32", baud=115200)
    with patch("nff.mcp_server.toolchain.flash", return_value="OK: flash complete"):
        result = await flash("void setup(){} void loop(){}")
    assert result == "OK: flash complete"


async def test_flash_returns_error_when_fqbn_missing(isolated_config):
    result = await flash("void setup(){}")
    assert result.startswith("ERROR:")


async def test_flash_passes_explicit_board_and_port():
    with patch("nff.mcp_server.toolchain.flash", return_value="OK: done") as mock_flash:
        await flash("void setup(){}", board="arduino:avr:uno", port="COM3")
    mock_flash.assert_called_once_with(
        code="void setup(){}", fqbn="arduino:avr:uno", port="COM3", source=None
    )


async def test_serial_read_returns_captured_text():
    with patch("nff.mcp_server.serial_module.serial_read", return_value="LED ON\nLED OFF\n"):
        result = await serial_read(duration_ms=500, port="COM10", baud=115200)
    assert "LED ON" in result


async def test_serial_read_passes_duration_port_baud():
    with patch("nff.mcp_server.serial_module.serial_read", return_value="") as mock_read:
        await serial_read(duration_ms=1000, port="COM10", baud=9600)
    mock_read.assert_called_once_with(1000, "COM10", 9600)


async def test_serial_write_returns_ok():
    with patch("nff.mcp_server.serial_module.serial_write",
               return_value="OK: wrote 5 byte(s) to COM10"):
        result = await serial_write("ping", port="COM10", baud=9600)
    assert result.startswith("OK:")


async def test_serial_write_returns_error_on_failure():
    with patch("nff.mcp_server.serial_module.serial_write",
               return_value="ERROR: port busy"):
        result = await serial_write("ping", port="COM99", baud=9600)
    assert result.startswith("ERROR:")


async def test_reset_device_returns_ok():
    with patch("nff.mcp_server.serial_module.reset_device",
               return_value="OK: reset COM10 via DTR toggle"):
        result = await reset_device(port="COM10")
    assert result.startswith("OK:")


async def test_reset_device_passes_port():
    with patch("nff.mcp_server.serial_module.reset_device",
               return_value="OK: reset COM10 via DTR toggle") as mock_reset:
        await reset_device(port="COM10")
    mock_reset.assert_called_once_with("COM10")


async def test_get_device_info_returns_full_info(isolated_config):
    from nff import config as cfg
    from nff.tools.boards import DetectedDevice
    cfg.set_default_device(port="COM10", board="ESP32 (CP210x)",
                           fqbn="esp32:esp32:esp32", baud=115200)
    mock_device = DetectedDevice(
        port="COM10", board="ESP32 (CP210x)",
        fqbn="esp32:esp32:esp32", vendor_id="10c4", product_id="ea60",
    )
    with patch("nff.mcp_server.boards_module.find_device", return_value=mock_device):
        result = await get_device_info(port="COM10")

    assert result["port"] == "COM10"
    assert result["board"] == "ESP32 (CP210x)"
    assert result["fqbn"] == "esp32:esp32:esp32"
    assert result["baud"] == 115200
    assert result["vendor_id"] == "10c4"


async def test_get_device_info_returns_error_when_no_port(isolated_config):
    result = await get_device_info(port=None)
    assert "error" in result


async def test_get_device_info_falls_back_to_config_when_board_unknown(isolated_config):
    from nff import config as cfg
    cfg.set_default_device(port="COM10", board="Custom Board",
                           fqbn="custom:x:x", baud=9600)
    with patch("nff.mcp_server.boards_module.find_device", return_value=None):
        result = await get_device_info(port="COM10")

    assert result["port"] == "COM10"
    assert result["board"] == "Custom Board"


# ===========================================================================
# HTTP INTEGRATION TESTS — full MCP protocol over HTTP
# ===========================================================================

_ALL_TOOL_NAMES = {
    "list_devices", "compile", "flash", "serial_read", "serial_write",
    "reset_device", "get_device_info",
    "authenticate", "complete_authentication",
    "auth_logout", "auth_status",
    "auth_clear", "auth_reconnect",
    "repair",
    # local crash classification (no login)
    "diagnose",
    # platform OTA / fleet (require login)
    "ota_deploy", "ota_status", "ota_deployments", "ota_devices", "fleet_status",
    # live on-chip debugging
    "debug_start", "debug_stop", "get_session_info", "get_call_stack",
    "get_variables", "expand_variable", "get_registers", "get_memory",
    "evaluate", "set_breakpoint", "pause_execution", "continue_execution",
    "step", "gdb_command",
    # energy measurement (nff-power-meter)
    "power_status", "power_measure",
}


# ---------------------------------------------------------------------------
# Bearer auth gate — HTTP transport level
# ---------------------------------------------------------------------------

async def test_mcp_transport_returns_401_without_token(base_url, monkeypatch):
    """With NFF_MCP_REQUIRE_AUTH on, POST to /mcp without a token returns 401."""
    monkeypatch.setenv("NFF_MCP_REQUIRE_AUTH", "1")
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/mcp", content=b"{}")
    assert resp.status_code == 401
    assert "www-authenticate" in resp.headers


async def test_mcp_transport_open_without_auth_by_default(base_url):
    """Gate is OFF by default: an unauthenticated /mcp POST is NOT rejected with 401."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/mcp", content=b"{}")
    assert resp.status_code != 401


async def test_mcp_transport_returns_401_with_wrong_token(base_url, monkeypatch):
    """With NFF_MCP_REQUIRE_AUTH on, POST to /mcp with a wrong token returns 401."""
    monkeypatch.setenv("NFF_MCP_REQUIRE_AUTH", "1")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/mcp",
            content=b"{}",
            headers={"Authorization": "Bearer wrong-token"},
        )
    assert resp.status_code == 401


async def test_mcp_transport_accepts_valid_bearer_token(mcp_url):
    """MCP session initializes when the correct Bearer token is provided."""
    from nff import config as cfg
    cfg.set_diagnosis_tokens(_TEST_TOKEN, _TEST_REFRESH)
    async with _authed_mcp_client(mcp_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            result = await session.initialize()
    assert result is not None


async def test_http_server_lists_all_tools(mcp_url):
    """initialize + tools/list over HTTP returns all registered tools."""
    from nff import config as cfg
    cfg.set_diagnosis_tokens(_TEST_TOKEN, _TEST_REFRESH)
    async with _authed_mcp_client(mcp_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()

    assert {t.name for t in result.tools} == _ALL_TOOL_NAMES


# ---------------------------------------------------------------------------
# OAuth 2.1 discovery and token endpoints
# ---------------------------------------------------------------------------

async def test_oauth_protected_resource(base_url):
    """/.well-known/oauth-protected-resource returns resource metadata JSON."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    data = resp.json()
    assert "resource" in data
    assert "127.0.0.1" in data["resource"]
    assert len(data["authorization_servers"]) == 1


async def test_oauth_authorization_server_metadata(base_url):
    """/.well-known/oauth-authorization-server returns authorization server metadata."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/.well-known/oauth-authorization-server")
    assert resp.status_code == 200
    data = resp.json()
    assert "authorization_endpoint" in data
    assert "token_endpoint" in data
    assert "registration_endpoint" in data
    assert "code" in data["response_types_supported"]


async def test_oauth_register(base_url):
    """POST /oauth/register returns a client_id for dynamic client registration."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/oauth/register", content=b"{}")
    assert resp.status_code == 201
    data = resp.json()
    assert "client_id" in data


async def test_oauth_authorize_missing_redirect_uri(base_url):
    """GET /oauth/authorize without redirect_uri returns 400."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/oauth/authorize")
    assert resp.status_code == 400


async def test_oauth_authorize_redirects_to_login(base_url):
    """GET /oauth/authorize redirects browser directly to /login with the OAuth callback URL."""
    redirect_uri = "http://127.0.0.1:9999/callback"
    async with httpx.AsyncClient(follow_redirects=False) as client:
        resp = await client.get(
            f"{base_url}/oauth/authorize",
            params={"redirect_uri": redirect_uri, "state": "test-state"},
        )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "/login" in location
    assert "/auth/portal" not in location
    # nff OAuth callback URL is percent-encoded in the cb query param
    assert "oauth" in location and "callback" in location


async def test_oauth_authorize_fast_paths_on_stored_mcp_token(base_url, isolated_config):
    """A persisted MCP token (no diagnosis token) short-circuits authorize.

    Cross-session persistence: a later session has only the stable MCP token in
    config (e.g. the Supabase JWT expired/was cleared). authorize must still
    issue a code for that SAME token instead of opening the browser, and the
    exchanged token must equal the stored one.
    """
    from nff import config as cfg
    cfg.set_mcp_tokens("nff_at_persisted", "nff_rt_persisted")
    redirect_uri = "http://127.0.0.1:9999/callback"

    async with httpx.AsyncClient(follow_redirects=False) as client:
        resp = await client.get(
            f"{base_url}/oauth/authorize",
            params={"redirect_uri": redirect_uri, "state": "s2"},
        )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(redirect_uri)  # back to client, not the login page
    assert "code=" in location and "state=s2" in location

    code = location.split("code=")[1].split("&")[0]
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            f"{base_url}/oauth/token",
            content=f"grant_type=authorization_code&code={code}",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert token_resp.json()["access_token"] == "nff_at_persisted"


async def test_oauth_callback_stores_tokens_and_redirects(base_url, isolated_config):
    """GET /oauth/callback/{id} stores tokens and redirects to client redirect_uri."""
    from nff.mcp_server import _oauth_sessions
    session_id = "testsession123"
    redirect_uri = "http://127.0.0.1:9999/callback"
    _oauth_sessions[session_id] = {"redirect_uri": redirect_uri, "state": "s1"}

    async with httpx.AsyncClient(follow_redirects=False) as client:
        resp = await client.get(
            f"{base_url}/oauth/callback/{session_id}",
            params={"access_token": "new_token", "refresh_token": "new_refresh"},
        )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "code=" in location
    assert "state=s1" in location

    from nff import config as cfg
    assert cfg.get_diagnosis_config()["access_token"] == "new_token"


async def test_oauth_token_exchange(base_url):
    """POST /oauth/token with a valid code returns the access_token."""
    from nff.mcp_server import _auth_codes
    _auth_codes["testcode"] = "mytoken"

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/oauth/token",
            content="grant_type=authorization_code&code=testcode",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["access_token"] == "mytoken"
    assert data["token_type"] == "bearer"
    assert "testcode" not in _auth_codes


async def test_oauth_token_invalid_code(base_url):
    """POST /oauth/token with an unknown code returns 400."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/oauth/token",
            content="grant_type=authorization_code&code=bogus",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert resp.status_code == 400


async def test_oauth_metadata_advertises_refresh_grant(base_url):
    """Auth-server metadata must advertise refresh_token so Claude refreshes silently."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/.well-known/oauth-authorization-server")
    assert resp.status_code == 200
    assert "refresh_token" in resp.json()["grant_types_supported"]


async def test_oauth_token_exchange_issues_refresh_token(base_url, isolated_config):
    """authorization_code exchange returns a refresh_token + long-lived expiry.

    Regression guard: without a refresh_token the MCP session died with the
    upstream Supabase JWT (~15 min), forcing re-auth.
    """
    from nff.mcp_server import _auth_codes, _MCP_TOKEN_TTL
    from nff import config as cfg
    cfg.set_mcp_tokens("nff_at_x", "nff_rt_y")
    _auth_codes["code1"] = "nff_at_x"

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/oauth/token",
            content="grant_type=authorization_code&code=code1",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["access_token"] == "nff_at_x"
    assert data["refresh_token"] == "nff_rt_y"
    assert data["expires_in"] == _MCP_TOKEN_TTL


async def test_oauth_refresh_token_grant_is_stable(base_url, isolated_config):
    """refresh_token grant returns the SAME persistent pair (no rotation).

    Stability is what lets login survive across sessions: Claude's stored
    credential keeps matching config until an explicit deauth, so a silent
    refresh never invalidates the token Claude already holds.
    """
    from nff import config as cfg
    cfg.set_mcp_tokens("nff_at_cur", "nff_rt_cur")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/oauth/token",
            content="grant_type=refresh_token&refresh_token=nff_rt_cur",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["access_token"] == "nff_at_cur"      # unchanged
    assert data["refresh_token"] == "nff_rt_cur"     # unchanged
    # config is untouched and still validates the token Claude already holds
    assert cfg.get_mcp_tokens()["access_token"] == "nff_at_cur"
    assert cfg.get_mcp_tokens()["refresh_token"] == "nff_rt_cur"


async def test_oauth_refresh_token_grant_rejects_stale(base_url, isolated_config):
    """A refresh_token that doesn't match the stored one is rejected."""
    from nff import config as cfg
    cfg.set_mcp_tokens("nff_at_cur", "nff_rt_cur")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/oauth/token",
            content="grant_type=refresh_token&refresh_token=nff_rt_stale",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert resp.status_code == 400


async def test_mcp_transport_accepts_opaque_mcp_token(mcp_url):
    """A bearer matching the stored opaque MCP access token is accepted at /mcp."""
    from nff import config as cfg
    cfg.set_mcp_tokens("nff_at_live", "nff_rt_live")
    async with _authed_mcp_client(mcp_url, token="nff_at_live") as (read, write, _):
        async with ClientSession(read, write) as session:
            result = await session.initialize()
    assert result is not None


# ---------------------------------------------------------------------------
# MCP tool calls — require valid Bearer token
# ---------------------------------------------------------------------------

async def test_http_call_list_devices_empty(mcp_url):
    """list_devices over HTTP returns empty devices list when no hardware is connected."""
    from nff import config as cfg
    cfg.set_diagnosis_tokens(_TEST_TOKEN, _TEST_REFRESH)
    with patch("nff.mcp_server.boards_module.list_devices", return_value=[]):
        async with _authed_mcp_client(mcp_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("list_devices", {})

    payload = json.loads(result.content[0].text)
    assert payload == {"devices": []}


async def test_http_call_list_devices_with_device(mcp_url):
    """list_devices over HTTP returns device data from boards module."""
    from nff import config as cfg
    cfg.set_diagnosis_tokens(_TEST_TOKEN, _TEST_REFRESH)
    from nff.tools.boards import DetectedDevice
    mock_device = DetectedDevice(
        port="COM10", board="ESP32 (CP210x)",
        fqbn="esp32:esp32:esp32", vendor_id="10c4", product_id="ea60",
    )
    with patch("nff.mcp_server.boards_module.list_devices", return_value=[mock_device]):
        async with _authed_mcp_client(mcp_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("list_devices", {})

    payload = json.loads(result.content[0].text)
    assert len(payload["devices"]) == 1
    assert payload["devices"][0]["port"] == "COM10"
    assert payload["devices"][0]["fqbn"] == "esp32:esp32:esp32"


async def test_http_call_flash_missing_config_returns_error(mcp_url):
    """flash over HTTP returns ERROR when no board/port is configured."""
    from nff import config as cfg
    cfg.set_diagnosis_tokens(_TEST_TOKEN, _TEST_REFRESH)
    async with _authed_mcp_client(mcp_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("flash", {"code": "void setup(){}"})

    assert result.content[0].text.startswith("ERROR:")


async def test_http_call_flash_with_mocked_toolchain(mcp_url):
    """flash over HTTP compiles and uploads when config and toolchain are mocked."""
    from nff import config as cfg
    cfg.set_diagnosis_tokens(_TEST_TOKEN, _TEST_REFRESH)
    cfg.set_default_device(port="COM10", board="ESP32 (CP210x)",
                           fqbn="esp32:esp32:esp32", baud=115200)
    with patch("nff.mcp_server.toolchain.flash", return_value="OK: flash complete"):
        async with _authed_mcp_client(mcp_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "flash", {"code": "void setup(){} void loop(){}"}
                )

    assert result.content[0].text == "OK: flash complete"


async def test_http_call_auth_status_authenticated(mcp_url):
    """auth_status over HTTP reports authenticated when a token is stored."""
    from nff import config as cfg
    cfg.set_diagnosis_tokens(_TEST_TOKEN, _TEST_REFRESH)
    async with _authed_mcp_client(mcp_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("auth_status", {})

    assert "ok: authenticated" in result.content[0].text.lower()


async def test_http_call_serial_read_with_mock(mcp_url):
    """serial_read over HTTP returns captured output from the serial module."""
    from nff import config as cfg
    cfg.set_diagnosis_tokens(_TEST_TOKEN, _TEST_REFRESH)
    with patch("nff.mcp_server.serial_module.serial_read", return_value="Hello World\n"):
        async with _authed_mcp_client(mcp_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "serial_read", {"duration_ms": 500, "port": "COM10", "baud": 115200}
                )

    assert "Hello World" in result.content[0].text


async def test_http_multiple_sequential_calls(mcp_url):
    """Multiple tool calls in one session all return correct results."""
    from nff import config as cfg
    cfg.set_diagnosis_tokens(_TEST_TOKEN, _TEST_REFRESH)
    with patch("nff.mcp_server.boards_module.list_devices", return_value=[]), \
         patch("nff.mcp_server.serial_module.reset_device",
               return_value="OK: reset COM10 via DTR toggle"):
        async with _authed_mcp_client(mcp_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                r1 = await session.call_tool("list_devices", {})
                r2 = await session.call_tool("reset_device", {"port": "COM10"})

    assert json.loads(r1.content[0].text) == {"devices": []}
    assert r2.content[0].text.startswith("OK:")


# ===========================================================================
# UNIT TESTS — auth_clear
# ===========================================================================

async def test_auth_clear_returns_ok(isolated_config):
    result = await auth_clear()
    assert result == "OK: tokens cleared"


async def test_auth_clear_wipes_existing_tokens(isolated_config):
    from nff import config as cfg
    cfg.set_diagnosis_tokens("acc123", "ref456")
    assert cfg.get_diagnosis_config()["access_token"] == "acc123"

    result = await auth_clear()

    assert result == "OK: tokens cleared"
    diag = cfg.get_diagnosis_config()
    assert diag.get("access_token") is None
    assert diag.get("refresh_token") is None


async def test_auth_clear_is_idempotent(isolated_config):
    """Calling auth_clear when already cleared still returns OK."""
    result = await auth_clear()
    assert result == "OK: tokens cleared"
    result2 = await auth_clear()
    assert result2 == "OK: tokens cleared"


# ===========================================================================
# UNIT TESTS — auth_reconnect
# ===========================================================================

def _make_token_response(access="new_access", refresh="new_refresh"):
    from nff.tools.auth import TokenResponse
    return TokenResponse(access_token=access, refresh_token=refresh, expires_in=3600)


async def test_auth_reconnect_direct_login_saves_tokens_and_re_registers(isolated_config):
    tokens = _make_token_response()
    mock_proc = type("P", (), {"returncode": 0})()
    with patch("nff.tools.auth.direct_login", return_value=tokens) as mock_login, \
         patch("subprocess.run", return_value=mock_proc) as mock_sub:
        result = await auth_reconnect(email="u@example.com", password="pw")

    mock_login.assert_called_once()
    mock_sub.assert_called_once()
    cmd = mock_sub.call_args[0][0]
    assert "claude" in cmd
    assert "--transport" in cmd
    assert "http" in cmd
    assert "--header" not in cmd  # OAuth flow manages the token; no Bearer in registration
    assert result.startswith("OK: reconnected")


async def test_auth_reconnect_saves_tokens_to_config(isolated_config):
    from nff import config as cfg
    tokens = _make_token_response("tok_a", "tok_r")
    mock_proc = type("P", (), {"returncode": 0})()
    with patch("nff.tools.auth.direct_login", return_value=tokens), \
         patch("subprocess.run", return_value=mock_proc):
        await auth_reconnect(email="u@example.com", password="pw")

    diag = cfg.get_diagnosis_config()
    assert diag["access_token"] == "tok_a"
    assert diag["refresh_token"] == "tok_r"


async def test_auth_reconnect_login_failure_returns_error(isolated_config):
    with patch("nff.tools.auth.direct_login", side_effect=Exception("bad credentials")):
        result = await auth_reconnect(email="u@example.com", password="wrong")
    assert result.startswith("ERROR:")
    assert "bad credentials" in result


async def test_auth_reconnect_requires_both_email_and_password(isolated_config):
    result = await auth_reconnect(email="only@example.com", password=None)
    assert result.startswith("ERROR:")


async def test_auth_reconnect_re_registration_failure_returns_partial_ok(isolated_config):
    """If claude mcp add fails, still reports authenticated but surfaces the manual command."""
    tokens = _make_token_response()
    mock_proc = type("P", (), {"returncode": 1, "stdout": "", "stderr": "error"})()
    with patch("nff.tools.auth.direct_login", return_value=tokens), \
         patch("subprocess.run", return_value=mock_proc):
        result = await auth_reconnect(email="u@example.com", password="pw")

    assert result.startswith("OK: authenticated but MCP re-registration failed")
    assert "claude mcp add" in result
    assert "--header" not in result  # No Bearer token in the suggested command


async def test_auth_reconnect_subprocess_exception_returns_partial_ok(isolated_config):
    """If subprocess.run raises, still reports authenticated with fallback message."""
    tokens = _make_token_response()
    with patch("nff.tools.auth.direct_login", return_value=tokens), \
         patch("subprocess.run", side_effect=FileNotFoundError("claude not found")):
        result = await auth_reconnect(email="u@example.com", password="pw")

    assert result.startswith("OK: authenticated but could not re-register MCP")


async def test_auth_reconnect_browser_flow_calls_oauth(isolated_config):
    """Omitting email+password triggers browser OAuth path."""
    import socket as _socket
    tokens = _make_token_response()
    mock_sock = _socket.socket()
    mock_proc = type("P", (), {"returncode": 0})()
    with patch("nff.tools.auth.bind_callback_server", return_value=(mock_sock, 9999)), \
         patch("nff.tools.auth.open_browser"), \
         patch("nff.tools.auth.wait_for_callback", return_value=tokens), \
         patch("subprocess.run", return_value=mock_proc):
        result = await auth_reconnect()

    assert result.startswith("OK: reconnected")


# ===========================================================================
# HTTP INTEGRATION — auth_clear & auth_reconnect
# ===========================================================================

async def test_http_auth_clear_clears_token(mcp_url):
    """auth_clear via the unit handler correctly clears the stored token."""
    from nff import config as cfg
    cfg.set_diagnosis_tokens("stale_token", "stale_refresh")
    result = await auth_clear()
    assert result == "OK: tokens cleared"
    assert cfg.get_diagnosis_config().get("access_token") is None


# ===========================================================================
# UNIT TESTS — authenticate (browser flow) & complete_authentication
# ===========================================================================

async def test_authenticate_browser_opens_login_url(isolated_config):
    """authenticate() with no args opens /login?cb=... and returns OK immediately."""
    import socket as _socket
    mock_sock = _socket.socket()
    with patch("nff.tools.auth.bind_callback_server", return_value=(mock_sock, 9999)) as mock_bind, \
         patch("nff.tools.auth.open_browser") as mock_browser:
        result = await authenticate()

    assert result.startswith("OK:")
    mock_browser.assert_called_once()
    opened_url: str = mock_browser.call_args[0][0]
    assert "/login" in opened_url
    assert "/auth/portal" not in opened_url
    assert "cb=" in opened_url
    assert "127.0.0.1" in opened_url


async def test_authenticate_browser_does_not_block(isolated_config):
    """authenticate() returns without waiting for the callback."""
    import socket as _socket
    mock_sock = _socket.socket()
    with patch("nff.tools.auth.bind_callback_server", return_value=(mock_sock, 9999)), \
         patch("nff.tools.auth.open_browser"), \
         patch("nff.tools.auth.wait_for_callback") as mock_wait:
        result = await authenticate()

    # wait_for_callback must NOT have been called — authenticate returns immediately
    mock_wait.assert_not_called()
    assert result.startswith("OK:")


async def test_complete_authentication_waits_for_callback(isolated_config):
    """complete_authentication() waits for the browser callback and saves tokens."""
    from nff import config as cfg
    from nff.tools.auth import TokenResponse
    import socket as _socket
    from nff.mcp_server import _pending_browser_auth

    mock_sock = _socket.socket()
    _pending_browser_auth["sock"] = mock_sock

    tokens = TokenResponse(access_token="tok_abc", refresh_token="ref_xyz")
    with patch("nff.tools.auth.wait_for_callback", return_value=tokens):
        result = await complete_authentication(timeout=5)

    assert result == "OK: authenticated"
    diag = cfg.get_diagnosis_config()
    assert diag["access_token"] == "tok_abc"
    assert diag["refresh_token"] == "ref_xyz"
    assert "sock" not in _pending_browser_auth


async def test_complete_authentication_no_pending_returns_error(isolated_config):
    """complete_authentication() with no pending session returns ERROR."""
    from nff.mcp_server import _pending_browser_auth
    _pending_browser_auth.clear()
    result = await complete_authentication()
    assert result.startswith("ERROR:")
    assert "authenticate" in result.lower()
