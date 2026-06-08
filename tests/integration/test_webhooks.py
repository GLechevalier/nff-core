"""Tests for the inbound /internal/ota-result webhook."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nff_ota import webhooks
from nff_ota.config import settings

_SECRET = "s3cret"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "fleet_secret", _SECRET)
    app = Starlette(routes=[Route("/internal/ota-result", webhooks._ota_result, methods=["POST"])])
    return TestClient(app)


def test_happy_path(client, monkeypatch):
    fake = AsyncMock(return_value={"finalized": True, "status": "success", "device_id": "test-device-01"})
    monkeypatch.setattr(webhooks, "finalize_ota_result", fake)
    resp = client.post(
        "/internal/ota-result",
        json={"device_id": "test-device-01", "status": "committed", "version": "2.0.0"},
        headers={"X-Fleet-Secret": _SECRET},
    )
    assert resp.status_code == 200
    assert resp.json()["finalized"] is True
    fake.assert_awaited_once_with("test-device-01", "committed", "2.0.0")


def test_bad_secret_401(client, monkeypatch):
    monkeypatch.setattr(webhooks, "finalize_ota_result", AsyncMock())
    resp = client.post(
        "/internal/ota-result",
        json={"device_id": "d", "status": "committed", "version": "2.0.0"},
        headers={"X-Fleet-Secret": "wrong"},
    )
    assert resp.status_code == 401


def test_missing_secret_401(client):
    resp = client.post("/internal/ota-result", json={"device_id": "d", "status": "committed", "version": "2.0.0"})
    assert resp.status_code == 401


def test_unconfigured_secret_rejects(monkeypatch):
    # When no secret is configured, even an empty header must NOT authenticate.
    monkeypatch.setattr(settings, "fleet_secret", "")
    app = Starlette(routes=[Route("/internal/ota-result", webhooks._ota_result, methods=["POST"])])
    c = TestClient(app)
    resp = c.post("/internal/ota-result", json={"device_id": "d", "status": "committed", "version": "2.0.0"},
                  headers={"X-Fleet-Secret": ""})
    assert resp.status_code == 401


def test_bad_status_400(client, monkeypatch):
    monkeypatch.setattr(webhooks, "finalize_ota_result", AsyncMock())
    resp = client.post(
        "/internal/ota-result",
        json={"device_id": "d", "status": "nonsense", "version": "2.0.0"},
        headers={"X-Fleet-Secret": _SECRET},
    )
    assert resp.status_code == 400


def test_missing_version_400(client, monkeypatch):
    monkeypatch.setattr(webhooks, "finalize_ota_result", AsyncMock())
    resp = client.post(
        "/internal/ota-result",
        json={"device_id": "d", "status": "committed"},
        headers={"X-Fleet-Secret": _SECRET},
    )
    assert resp.status_code == 400


def test_invalid_json_400(client, monkeypatch):
    monkeypatch.setattr(webhooks, "finalize_ota_result", AsyncMock())
    resp = client.post(
        "/internal/ota-result",
        content="not json",
        headers={"X-Fleet-Secret": _SECRET, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
