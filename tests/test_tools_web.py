"""Unit tests for web tools: fuzz_web_paths and test_default_creds_http."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from nff_pentester.tools.web.auth_bypass import AuthBypassTesterInput, auth_bypass_tester
from nff_pentester.tools.web.csrf import CsrfAction, CsrfTesterInput, csrf_tester
from nff_pentester.tools.web.default_creds import (
    TestDefaultCredsHttpInput,
    test_default_creds_http as _test_default_creds_http,
)
from nff_pentester.tools.web.fuzzer import FuzzWebPathsInput, fuzz_web_paths
from nff_pentester.tools.web.idor import IdorTesterInput, idor_tester
from nff_pentester.tools.web.traversal import (
    DirectoryTraversalTesterInput,
    directory_traversal_tester,
)

_PATCH_FUZZER_CLIENT = "nff_pentester.tools.web.fuzzer.httpx.AsyncClient"
_PATCH_CREDS_CLIENT = "nff_pentester.tools.web.default_creds.httpx.AsyncClient"
_PATCH_TRAVERSAL_CLIENT = "nff_pentester.tools.web.traversal.httpx.AsyncClient"
_PATCH_AUTHBYPASS_CLIENT = "nff_pentester.tools.web.auth_bypass.httpx.AsyncClient"
_PATCH_IDOR_CLIENT = "nff_pentester.tools.web.idor.httpx.AsyncClient"
_PATCH_CSRF_CLIENT = "nff_pentester.tools.web.csrf.httpx.AsyncClient"


def _seq_get_client(responses):
    """A mocked AsyncClient whose .get/.request return the given responses in sequence."""
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    client.get = AsyncMock(side_effect=list(responses))
    client.request = AsyncMock(side_effect=list(responses))
    return client


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


# ── fuzz_web_paths methods (POST/PUT/DELETE) ────────────────────────────────────

async def test_fuzz_web_paths_multiple_methods(mock_ctx):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    client.get = AsyncMock(return_value=_make_response(200))
    client.post = AsyncMock(return_value=_make_response(201))
    with patch(_PATCH_FUZZER_CLIENT, return_value=client):
        params = FuzzWebPathsInput(
            base_url="http://192.168.1.1", wordlist=["/api"], methods=["GET", "POST"]
        )
        result = await fuzz_web_paths(params, mock_ctx)
    methods = {h.method for h in result.hits}
    assert methods == {"GET", "POST"}
    client.post.assert_awaited()


# ── directory_traversal_tester ──────────────────────────────────────────────────

async def test_directory_traversal_detected(mock_ctx):
    resp = _make_response(200, text="root:x:0:0:root:/root:/bin/sh\n")
    client = _make_http_client(get_response=resp)
    with patch(_PATCH_TRAVERSAL_CLIENT, return_value=client):
        params = DirectoryTraversalTesterInput(
            base_url="http://192.168.1.1", paths=["/download"],
            param_fields=["file"], payloads=["../../etc/passwd"],
        )
        result = await directory_traversal_tester(params, mock_ctx)
    assert result.vulnerable is True
    assert result.evidence[0].marker.startswith("root:")


async def test_directory_traversal_clean(mock_ctx):
    resp = _make_response(200, text="<html>not found</html>")
    client = _make_http_client(get_response=resp)
    with patch(_PATCH_TRAVERSAL_CLIENT, return_value=client):
        params = DirectoryTraversalTesterInput(
            base_url="http://192.168.1.1", paths=["/download"],
            param_fields=["file"], payloads=["../../etc/passwd"],
        )
        result = await directory_traversal_tester(params, mock_ctx)
    assert result.vulnerable is False


async def test_directory_traversal_connect_error_continues(mock_ctx):
    client = _make_http_client(get_side_effect=httpx.ConnectError("refused"))
    with patch(_PATCH_TRAVERSAL_CLIENT, return_value=client):
        params = DirectoryTraversalTesterInput(
            base_url="http://192.168.1.1", paths=["/download"],
            param_fields=["file"], payloads=["../../etc/passwd"],
        )
        result = await directory_traversal_tester(params, mock_ctx)
    assert result.vulnerable is False
    assert result.error is None


# ── auth_bypass_tester ──────────────────────────────────────────────────────────

async def test_auth_bypass_direct_no_auth(mock_ctx):
    # Baseline itself returns 200 → endpoint is open with no session.
    client = _make_http_client(get_response=_make_response(200, text="admin panel"))
    with patch(_PATCH_AUTHBYPASS_CLIENT, return_value=client):
        params = AuthBypassTesterInput(base_url="http://192.168.1.1", paths=["/admin"])
        result = await auth_bypass_tester(params, mock_ctx)
    assert result.bypassed is True
    assert result.evidence[0].technique == "direct-no-auth"


async def test_auth_bypass_via_technique(mock_ctx):
    # Baseline 403 (protected), then every technique returns 200.
    responses = [_make_response(403)] + [_make_response(200) for _ in range(4)]
    client = _seq_get_client(responses)
    with patch(_PATCH_AUTHBYPASS_CLIENT, return_value=client):
        params = AuthBypassTesterInput(base_url="http://192.168.1.1", paths=["/admin"])
        result = await auth_bypass_tester(params, mock_ctx)
    assert result.bypassed is True
    techniques = {e.technique for e in result.evidence}
    assert "jwt-alg-none" in techniques


async def test_auth_bypass_properly_protected(mock_ctx):
    responses = [_make_response(403) for _ in range(5)]
    client = _seq_get_client(responses)
    with patch(_PATCH_AUTHBYPASS_CLIENT, return_value=client):
        params = AuthBypassTesterInput(base_url="http://192.168.1.1", paths=["/admin"])
        result = await auth_bypass_tester(params, mock_ctx)
    assert result.bypassed is False


# ── idor_tester ─────────────────────────────────────────────────────────────────

async def test_idor_detected(mock_ctx):
    responses = [
        _make_response(200, text="objA"),  # baseline
        _make_response(200, text="objB"),  # probe distinct
    ]
    client = _seq_get_client(responses)
    with patch(_PATCH_IDOR_CLIENT, return_value=client):
        params = IdorTesterInput(
            base_url="http://192.168.1.1",
            endpoint_template="/api/device/{id}",
            baseline_id="1",
            probe_ids=["2"],
        )
        result = await idor_tester(params, mock_ctx)
    assert result.idor_found is True
    assert result.evidence[0].probe_id == "2"


async def test_idor_not_found_when_blocked(mock_ctx):
    responses = [
        _make_response(200, text="objA"),  # baseline
        _make_response(403, text="forbidden"),  # probe blocked
    ]
    client = _seq_get_client(responses)
    with patch(_PATCH_IDOR_CLIENT, return_value=client):
        params = IdorTesterInput(
            base_url="http://192.168.1.1",
            endpoint_template="/api/device/{id}",
            baseline_id="1",
            probe_ids=["2"],
        )
        result = await idor_tester(params, mock_ctx)
    assert result.idor_found is False


# ── csrf_tester ─────────────────────────────────────────────────────────────────

def _csrf_client(page_text="<form></form>", action_status=200):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    client.get = AsyncMock(return_value=_make_response(200, text=page_text))
    client.request = AsyncMock(return_value=_make_response(action_status, text="ok"))
    return client


async def test_csrf_vulnerable_no_token(mock_ctx):
    client = _csrf_client(page_text="<form><input name='x'></form>", action_status=200)
    with patch(_PATCH_CSRF_CLIENT, return_value=client):
        params = CsrfTesterInput(
            base_url="http://192.168.1.1",
            actions=[CsrfAction(method="POST", path="/reboot")],
        )
        result = await csrf_tester(params, mock_ctx)
    assert result.csrf_vulnerable is True
    assert result.evidence[0].accepted is True
    assert result.evidence[0].token_seen is False


async def test_csrf_protected_by_token(mock_ctx):
    client = _csrf_client(
        page_text="<form><input name='csrf_token' value='abc'></form>", action_status=200
    )
    with patch(_PATCH_CSRF_CLIENT, return_value=client):
        params = CsrfTesterInput(
            base_url="http://192.168.1.1",
            actions=[CsrfAction(method="POST", path="/reboot")],
        )
        result = await csrf_tester(params, mock_ctx)
    assert result.csrf_vulnerable is False
    assert result.evidence[0].token_seen is True
