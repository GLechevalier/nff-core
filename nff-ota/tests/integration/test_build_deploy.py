"""Tests for the inbound /internal/build-deploy webhook (used by nff-github)."""
from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

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
def client(monkeypatch, mock_supabase):
    monkeypatch.setattr(settings, "fleet_secret", _SECRET)
    # The route resolves the group's project_id (tenant anchor) before uploading; serve
    # it from the mock so the test never needs a live Supabase.
    mock_supabase.table.return_value.execute = AsyncMock(
        return_value=MagicMock(data=[{"id": "grp-1", "project_id": "proj-1"}], error=None)
    )
    app = Starlette(
        routes=[Route("/internal/build-deploy", webhooks._build_deploy, methods=["POST"])]
    )
    return TestClient(app)


def _patch_pipeline(monkeypatch, *, dep_id="dep-1", mock_only=False):
    """Stub the ops the route orchestrates; return the mocks."""
    upload = AsyncMock(return_value=ArtifactResult(error=None))
    create = AsyncMock(return_value=DeploymentResult(deployment=_deployment(dep_id)))
    start = AsyncMock(return_value=StartDeploymentResult(delivered=2, failed=0, skipped=0))
    monkeypatch.setattr(webhooks, "upload_artifact_impl", upload)
    monkeypatch.setattr(webhooks, "create_deployment", create)
    monkeypatch.setattr(webhooks, "start_deployment", start)
    monkeypatch.setattr(webhooks, "group_is_mock_only", AsyncMock(return_value=mock_only))
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


def test_device_types_derived_from_group(client, monkeypatch):
    """No device_types in the body -> derived from the target group's members."""
    upload, create, start = _patch_pipeline(monkeypatch)
    derive = AsyncMock(return_value=["esp32", "esp32s3"])
    monkeypatch.setattr(webhooks, "group_member_device_types", derive)
    body = {
        "version": "fw-abc1234",
        "name": "fw-abc1234",
        "artifact_b64": base64.b64encode(b"\x00binary").decode(),
        "device_group_id": "grp-1",
    }
    resp = client.post("/internal/build-deploy", json=body, headers={"X-Fleet-Secret": _SECRET})
    assert resp.status_code == 200
    derive.assert_awaited_once_with("grp-1")
    # the derived list is what gets recorded on the artifact
    assert upload.await_args.args[0].device_types == ["esp32", "esp32s3"]


def test_explicit_device_types_skip_derivation(client, monkeypatch):
    """An explicit non-empty list is respected and the group is not queried."""
    upload, create, start = _patch_pipeline(monkeypatch)
    derive = AsyncMock(return_value=["esp32s3"])
    monkeypatch.setattr(webhooks, "group_member_device_types", derive)
    body = {
        "version": "fw-abc1234", "name": "fw-abc1234", "device_types": ["esp32"],
        "artifact_b64": base64.b64encode(b"\x00binary").decode(), "device_group_id": "grp-1",
    }
    resp = client.post("/internal/build-deploy", json=body, headers={"X-Fleet-Secret": _SECRET})
    assert resp.status_code == 200
    derive.assert_not_awaited()
    assert upload.await_args.args[0].device_types == ["esp32"]


def test_empty_device_types_and_undescribed_group_400(client, monkeypatch):
    """No device_types and a group whose members have no device_type -> 400."""
    _patch_pipeline(monkeypatch)
    monkeypatch.setattr(webhooks, "group_member_device_types", AsyncMock(return_value=[]))
    body = {
        "version": "fw-abc1234", "name": "fw-abc1234",
        "artifact_b64": base64.b64encode(b"\x00binary").decode(), "device_group_id": "grp-1",
    }
    resp = client.post("/internal/build-deploy", json=body, headers={"X-Fleet-Secret": _SECRET})
    assert resp.status_code == 400
    assert "derived from the target group" in resp.json()["error"]


def test_build_id_elf_and_lineage_are_threaded(client, monkeypatch):
    """The connector's build_id / ELF / git lineage reach upload_artifact + storage."""
    upload, create, start = _patch_pipeline(monkeypatch)
    store_elf = AsyncMock(return_value="firmware/fw-abc1234.elf")
    monkeypatch.setattr(webhooks, "upload_elf", store_elf)
    body = {
        "version": "fw-abc1234",
        "name": "Fix sensor drift",
        "device_types": ["esp32"],
        "artifact_b64": base64.b64encode(b"\x00binary").decode(),
        "elf_b64": base64.b64encode(b"\x7fELFdebug").decode(),
        "device_group_id": "grp-1",
        "build_id": "abcdef1234567890",
        "branch_name": "main",
        "author": "octocat",
        "committed_at": "2026-06-09T07:26:55Z",
        "changelog": "Fix sensor drift",
        "parent_version": "fw-0000000",
        "release_version": "3.0.13",
    }
    resp = client.post("/internal/build-deploy", json=body, headers={"X-Fleet-Secret": _SECRET})
    assert resp.status_code == 200

    store_elf.assert_awaited_once()
    assert store_elf.await_args.args[0] == "fw-abc1234"  # keyed by version

    params = upload.await_args.args[0]
    assert params.build_id == "abcdef1234567890"
    assert params.branch_name == "main"
    assert params.author == "octocat"
    assert params.committed_at == "2026-06-09T07:26:55Z"
    assert params.changelog == "Fix sensor drift"
    assert params.parent_version == "fw-0000000"
    assert params.elf_path == "firmware/fw-abc1234.elf"
    assert params.release_version == "3.0.13"


def test_bad_elf_b64_surfaces_400(client, monkeypatch):
    _patch_pipeline(monkeypatch)
    monkeypatch.setattr(webhooks, "upload_elf", AsyncMock())
    body = {
        "version": "fw-abc1234", "name": "v", "device_types": ["esp32"],
        "artifact_b64": base64.b64encode(b"\x00binary").decode(),
        "elf_b64": "not valid base64!!", "device_group_id": "grp-1",
    }
    resp = client.post("/internal/build-deploy", json=body, headers={"X-Fleet-Secret": _SECRET})
    assert resp.status_code == 400
    assert "elf_b64" in resp.json()["error"]


def test_no_lineage_leaves_params_unset(client, monkeypatch):
    """A bare upload (no build_id/elf/lineage) passes None through — never invents values."""
    upload, create, start = _patch_pipeline(monkeypatch)
    monkeypatch.setattr(webhooks, "upload_elf", AsyncMock())
    body = {
        "version": "fw-abc1234", "name": "v", "device_types": ["esp32"],
        "artifact_b64": base64.b64encode(b"\x00binary").decode(), "device_group_id": "grp-1",
    }
    resp = client.post("/internal/build-deploy", json=body, headers={"X-Fleet-Secret": _SECRET})
    assert resp.status_code == 200
    params = upload.await_args.args[0]
    assert params.build_id is None
    assert params.elf_path is None
    assert params.parent_version is None


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


def test_mock_only_group_allows_synthetic_upload(client, monkeypatch):
    """A mock-only target group makes the upload synthetic-permissive (image gate off)."""
    upload, _, _ = _patch_pipeline(monkeypatch, mock_only=True)
    body = {
        "version": "fw-abc1234", "name": "v", "device_types": ["esp32"],
        "artifact_b64": base64.b64encode(b"not firmware").decode(), "device_group_id": "grp-1",
    }
    resp = client.post("/internal/build-deploy", json=body, headers={"X-Fleet-Secret": _SECRET})
    assert resp.status_code == 200
    assert upload.await_args.kwargs["allow_synthetic"] is True


def test_real_group_upload_is_strict(client, monkeypatch):
    """A group with real/QEMU hardware keeps the ESP32 image gate on (allow_synthetic=False)."""
    upload, _, _ = _patch_pipeline(monkeypatch, mock_only=False)
    body = {
        "version": "fw-abc1234", "name": "v", "device_types": ["esp32"],
        "artifact_b64": base64.b64encode(b"\xe9binary").decode(), "device_group_id": "grp-1",
    }
    resp = client.post("/internal/build-deploy", json=body, headers={"X-Fleet-Secret": _SECRET})
    assert resp.status_code == 200
    assert upload.await_args.kwargs["allow_synthetic"] is False
