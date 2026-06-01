"""Unit tests for web tools: fuzz_web_paths and test_default_creds_http."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from nff_pentester.tools.web.default_creds import (
    TestDefaultCredsHttpInput,
    test_default_creds_http as _test_default_creds_http,
)
from nff_pentester.tools.web.fuzzer import FuzzWebPathsInput, fuzz_web_paths

_PATCH_FUZZER_CLIENT = "nff_pentester.tools.web.fuzzer.httpx.AsyncClient"
_PATCH_CREDS_CLIENT = "nff_pentester.tools.web.default_creds.httpx.AsyncClient"


def _make_http_client(get_response=None, post_response=None, get_side_effect=None, post_side_effect=None):
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    if get_response is not None:
        mock_client.get = AsyncMock(return_value=get_response)
    if get_side_effect is not None:
        mock_client.get = AsyncMock(side_effect=get_side_effect)
    if post_response is not None:
        mock_client.post = AsyncMock(return_value=post_response)
    if post_side_effect is not None:
        mock_client.post = AsyncMock(side_effect=post_side_effect)
    return mock_client


def _make_response(status_code=200, text="", headers=None, content=b""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.content = content or text.encode()
    resp.headers = headers or {"content-type": "text/html", "content-length": "100"}
    return resp


# ── fuzz_web_paths ─────────────────────────────────────────────────────────────

async def test_fuzz_web_paths_hit_returned(mock_ctx):
    resp = _make_response(200)
    client = _make_http_client(get_response=resp)
    with patch(_PATCH_FUZZER_CLIENT, return_value=client):
        params = FuzzWebPathsInput(base_url="http://192.168.1.1", wordlist=["/admin"])
        result = await fuzz_web_paths(params, mock_ctx)
    assert len(result.hits) == 1
    assert result.hits[0].path == "/admin"
    assert result.hits[0].status_code == 200


async def test_fuzz_web_paths_filtered_out(mock_ctx):
    resp = _make_response(404)
    client = _make_http_client(get_response=resp)
    with patch(_PATCH_FUZZER_CLIENT, return_value=client):
        params = FuzzWebPathsInput(base_url="http://192.168.1.1", wordlist=["/missing"])
        result = await fuzz_web_paths(params, mock_ctx)
    assert result.hits == []


async def test_fuzz_web_paths_custom_wordlist_paths_tested(mock_ctx):
    resp = _make_response(200)
    client = _make_http_client(get_response=resp)
    with patch(_PATCH_FUZZER_CLIENT, return_value=client):
        params = FuzzWebPathsInput(base_url="http://192.168.1.1", wordlist=["/admin", "/login", "/api"])
        result = await fuzz_web_paths(params, mock_ctx)
    assert result.paths_tested == 3


async def test_fuzz_web_paths_connect_error_continues(mock_ctx):
    client = _make_http_client(get_side_effect=httpx.ConnectError("refused"))
    with patch(_PATCH_FUZZER_CLIENT, return_value=client):
        params = FuzzWebPathsInput(base_url="http://192.168.1.1", wordlist=["/admin", "/login"])
        result = await fuzz_web_paths(params, mock_ctx)
    assert result.hits == []
    assert result.error is None


async def test_fuzz_web_paths_multiple_status_codes(mock_ctx):
    responses = [_make_response(200), _make_response(401), _make_response(403), _make_response(404)]
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.get = AsyncMock(side_effect=responses)
    with patch(_PATCH_FUZZER_CLIENT, return_value=mock_client):
        params = FuzzWebPathsInput(
            base_url="http://192.168.1.1",
            wordlist=["/admin", "/login", "/secure", "/missing"],
            status_filter=[200, 401, 403],
        )
        result = await fuzz_web_paths(params, mock_ctx)
    assert len(result.hits) == 3
    status_codes = {h.status_code for h in result.hits}
    assert status_codes == {200, 401, 403}


async def test_fuzz_web_paths_base_url_trailing_slash_stripped(mock_ctx):
    resp = _make_response(200)
    client = _make_http_client(get_response=resp)
    with patch(_PATCH_FUZZER_CLIENT, return_value=client):
        params = FuzzWebPathsInput(base_url="http://192.168.1.1/", wordlist=["/admin"])
        result = await fuzz_web_paths(params, mock_ctx)
    assert result.base_url == "http://192.168.1.1"


# ── test_default_creds_http ────────────────────────────────────────────────────

async def test_default_creds_http_success_by_status(mock_ctx):
    resp = _make_response(200, text="dashboard", content=b"dashboard")
    client = _make_http_client(post_response=resp)
    with patch(_PATCH_CREDS_CLIENT, return_value=client):
        params = TestDefaultCredsHttpInput(
            login_url="http://192.168.1.1/login",
            credentials=[("admin", "admin")],
            failure_status_codes=[401, 403],
        )
        result = await _test_default_creds_http(params, mock_ctx)
    assert result.successful_credentials == [("admin", "admin")]
    assert result.attempts == 1


async def test_default_creds_http_failure_by_status(mock_ctx):
    resp = _make_response(401, text="Unauthorized", content=b"Unauthorized")
    client = _make_http_client(post_response=resp)
    with patch(_PATCH_CREDS_CLIENT, return_value=client):
        params = TestDefaultCredsHttpInput(
            login_url="http://192.168.1.1/login",
            credentials=[("admin", "wrongpass")],
        )
        result = await _test_default_creds_http(params, mock_ctx)
    assert result.successful_credentials == []


async def test_default_creds_http_success_with_body_indicator(mock_ctx):
    resp = _make_response(200, text="Welcome to admin panel", content=b"Welcome to admin panel")
    client = _make_http_client(post_response=resp)
    with patch(_PATCH_CREDS_CLIENT, return_value=client):
        params = TestDefaultCredsHttpInput(
            login_url="http://192.168.1.1/login",
            credentials=[("admin", "admin")],
            success_indicator="Welcome",
        )
        result = await _test_default_creds_http(params, mock_ctx)
    assert result.successful_credentials == [("admin", "admin")]


async def test_default_creds_http_body_indicator_not_found(mock_ctx):
    resp = _make_response(200, text="Login failed", content=b"Login failed")
    client = _make_http_client(post_response=resp)
    with patch(_PATCH_CREDS_CLIENT, return_value=client):
        params = TestDefaultCredsHttpInput(
            login_url="http://192.168.1.1/login",
            credentials=[("admin", "admin")],
            success_indicator="Welcome",
        )
        result = await _test_default_creds_http(params, mock_ctx)
    assert result.successful_credentials == []


async def test_default_creds_http_multiple_credentials(mock_ctx):
    responses = [_make_response(401), _make_response(401), _make_response(200)]
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post = AsyncMock(side_effect=responses)
    with patch(_PATCH_CREDS_CLIENT, return_value=mock_client):
        params = TestDefaultCredsHttpInput(
            login_url="http://192.168.1.1/login",
            credentials=[("admin", "wrong1"), ("admin", "wrong2"), ("admin", "correct")],
        )
        result = await _test_default_creds_http(params, mock_ctx)
    assert result.attempts == 3
    assert result.successful_credentials == [("admin", "correct")]
