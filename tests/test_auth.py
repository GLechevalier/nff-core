from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from nff_diagnosis.server.app import create_app

_SUPABASE_ENV = {"SUPABASE_URL": "https://fake.supabase.co", "SUPABASE_ANON_KEY": "anon-key"}
_TOKEN_PAYLOAD = {
    "access_token": "acc.tok",
    "refresh_token": "ref.tok",
    "token_type": "bearer",
    "expires_in": 3600,
}


@pytest.fixture()
def app_client(tmp_path: Path):
    with patch("nff_diagnosis.engine.diagnosis.anthropic.Anthropic", MagicMock(return_value=MagicMock())):
        app = create_app(
            store_root=tmp_path / "store",
            db_path=tmp_path / "test.db",
            api_key="fake",
        )
        with TestClient(app) as c:
            yield app, c


def _make_human_identity(owner_id: str = "user-123"):
    from nff_diagnosis.auth.models import Identity

    async def fake_resolve(auth):
        if auth and auth.startswith("Bearer human-"):
            uid = auth[len("Bearer human-"):]
            return Identity(owner_id=uid)
        return None

    return patch("nff_diagnosis.auth.dependencies._resolve", side_effect=fake_resolve)


def _mock_http_post(status_code: int, body: dict):
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = body

    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.post = AsyncMock(return_value=mock_resp)
    return mock_http


# ---------------------------------------------------------------------------
# POST /api/auth/login
# ---------------------------------------------------------------------------


def test_login_success(app_client):
    _, client = app_client
    mock_http = _mock_http_post(200, _TOKEN_PAYLOAD)
    with patch.dict("os.environ", _SUPABASE_ENV), \
         patch("nff_diagnosis.auth.router.httpx.AsyncClient", return_value=mock_http):
        resp = client.post("/api/auth/login", json={"email": "a@b.com", "password": "secret"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["access_token"] == "acc.tok"
    assert data["refresh_token"] == "ref.tok"
    assert data["expires_in"] == 3600


def test_login_wrong_password(app_client):
    _, client = app_client
    mock_http = _mock_http_post(400, {"error_description": "Invalid login credentials"})
    with patch.dict("os.environ", _SUPABASE_ENV), \
         patch("nff_diagnosis.auth.router.httpx.AsyncClient", return_value=mock_http):
        resp = client.post("/api/auth/login", json={"email": "a@b.com", "password": "wrong"})
    assert resp.status_code == 401
    assert "Invalid login credentials" in resp.json()["detail"]


def test_login_no_supabase_config(app_client):
    _, client = app_client
    resp = client.post("/api/auth/login", json={"email": "a@b.com", "password": "x"})
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# POST /api/auth/refresh
# ---------------------------------------------------------------------------


def test_refresh_success(app_client):
    _, client = app_client
    new_payload = {**_TOKEN_PAYLOAD, "access_token": "new.acc", "refresh_token": "new.ref"}
    mock_http = _mock_http_post(200, new_payload)
    with patch.dict("os.environ", _SUPABASE_ENV), \
         patch("nff_diagnosis.auth.router.httpx.AsyncClient", return_value=mock_http):
        resp = client.post("/api/auth/refresh", json={"refresh_token": "old.ref"})
    assert resp.status_code == 200
    assert resp.json()["access_token"] == "new.acc"


def test_refresh_invalid_token(app_client):
    _, client = app_client
    mock_http = _mock_http_post(401, {"error_description": "Invalid refresh token"})
    with patch.dict("os.environ", _SUPABASE_ENV), \
         patch("nff_diagnosis.auth.router.httpx.AsyncClient", return_value=mock_http):
        resp = client.post("/api/auth/refresh", json={"refresh_token": "bad"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/auth/logout
# ---------------------------------------------------------------------------


def test_logout_success(app_client):
    _, client = app_client
    mock_http = _mock_http_post(204, {})
    with _make_human_identity(), \
         patch.dict("os.environ", _SUPABASE_ENV), \
         patch("nff_diagnosis.auth.router.httpx.AsyncClient", return_value=mock_http):
        resp = client.post("/api/auth/logout", headers={"Authorization": "Bearer human-u1"})
    assert resp.status_code == 204


def test_logout_unauthenticated(app_client):
    _, client = app_client
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 401


