"""Tests for the inbound /internal/build-deploy webhook (used by nff-github)."""
from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nff_ota import webhooks
from nff_ota.config import settings
from nff_ota.models.artifact import ArtifactResult
from nff_ota.models.deployment import DeploymentInfo, DeploymentResult, StartDeploymentResult

_SECRET = "s3cret"


def _deployment(dep_id: str = "dep-1") -> DeploymentInfo:
    return DeploymentInfo(
        id=dep_id, name="v", target_version="fw-abc1234", device_group_id="grp-1",
        status="active", max_devices_in_flight=10, retries=3, created_at="t", start_time=None,
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "fleet_secret", _SECRET)
    app = Starlette(
        routes=[Route("/internal/build-deploy", webhooks._build_deploy, methods=["POST"])]
    )
    return TestClient(app)


def _patch_pipeline(monkeypatch, *, dep_id="dep-1"):
    """Stub the three nff-ota ops the route orchestrates; return the mocks."""
    upload = AsyncMock(return_value=ArtifactResult(error=None))
    create = AsyncMock(return_value=DeploymentResult(deployment=_deployment(dep_id)))
    start = AsyncMock(return_value=StartDeploymentResult(delivered=2, failed=0, skipped=0))
    monkeypatch.setattr(webhooks, "upload_artifact", upload)
    monkeypatch.setattr(webhooks, "create_deployment", create)
    monkeypatch.setattr(webhooks, "start_deployment", start)
    return upload, create, start


def test_happy_path_with_b64_artifact(client, monkeypatch):
    upload, create, start = _patch_pipeline(monkeypatch)
    body = {
        "version": "fw-abc1234",
        "name": "fw-abc1234",
        "device_types": ["esp32"],
        "artifact_b64": base64.b64encode(b"\x00binary").decode(),
        "device_group_id": "grp-1",
    }
    resp = client.post("/internal/build-deploy", json=body, headers={"X-Fleet-Secret": _SECRET})
    assert resp.status_code == 200
    out = resp.json()
    assert out["deployment_id"] == "dep-1"
    assert out["version"] == "fw-abc1234"
    assert out["delivered"] == 2
    # upload got a real temp file path; create+start were called once each.
    upload.assert_awaited_once()
    create.assert_awaited_once()
    start.assert_awaited_once_with("dep-1")


def test_bad_secret_401(client, monkeypatch):
    _patch_pipeline(monkeypatch)
    resp = client.post(
        "/internal/build-deploy",
        json={"version": "v", "name": "v", "device_types": ["esp32"],
              "artifact_b64": "AA==", "device_group_id": "grp-1"},
        headers={"X-Fleet-Secret": "wrong"},
    )
    assert resp.status_code == 401


def test_missing_artifact_400(client, monkeypatch):
    _patch_pipeline(monkeypatch)
    resp = client.post(
        "/internal/build-deploy",
        json={"version": "v", "name": "v", "device_types": ["esp32"], "device_group_id": "grp-1"},
        headers={"X-Fleet-Secret": _SECRET},
    )
    assert resp.status_code == 400


def test_missing_group_400(client, monkeypatch):
    _patch_pipeline(monkeypatch)
    resp = client.post(
        "/internal/build-deploy",
        json={"version": "v", "name": "v", "device_types": ["esp32"], "artifact_b64": "AA=="},
        headers={"X-Fleet-Secret": _SECRET},
    )
    assert resp.status_code == 400


def test_upload_failure_surfaces_400(client, monkeypatch):
    upload, create, start = _patch_pipeline(monkeypatch)
    upload.return_value = ArtifactResult(error="Storage upload failed: boom")
    resp = client.post(
        "/internal/build-deploy",
        json={"version": "v", "name": "v", "device_types": ["esp32"],
              "artifact_b64": "AA==", "device_group_id": "grp-1"},
        headers={"X-Fleet-Secret": _SECRET},
    )
    assert resp.status_code == 400
    assert "upload failed" in resp.json()["error"]
    create.assert_not_awaited()
