"""Integration tests for artifact tools using a mocked Supabase client."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nff_ota.crypto import sha256_bytes
from nff_ota.models.artifact import UploadArtifactParams
from nff_ota.tools.artifacts.ops import delete_artifact, list_artifacts, upload_artifact


def _make_artifact_row(
    artifact_id: str,
    name: str = "fw",
    version: str = "1.0.0",
    device_types: list[str] | None = None,
    checksum: str = "abc",
) -> dict:
    return {
        "id": artifact_id,
        "name": name,
        "version": version,
        "device_types": device_types or ["esp32"],
        "checksum_sha256": checksum,
        "size_bytes": 1024,
        "signature": None,
        "storage_path": f"{artifact_id[:2]}/{artifact_id}.bin",
        "provides": {},
        "depends": {},
        "created_at": "2026-01-01T00:00:00+00:00",
    }


class TestUploadArtifact:
    async def test_upload_stores_and_returns_artifact(
        self, mock_supabase, sample_artifact_bytes, tmp_path
    ):
        fw_file = tmp_path / "fw.bin"
        fw_file.write_bytes(sample_artifact_bytes)
        expected_checksum = sha256_bytes(sample_artifact_bytes)

        # Make insert return a row with the computed checksum
        def fake_execute():
            resp = MagicMock()
            resp.data = [{
                "id": "test-id",
                "name": "fw-esp32",
                "version": "1.0.0",
                "device_types": ["esp32"],
                "checksum_sha256": expected_checksum,
                "size_bytes": len(sample_artifact_bytes),
                "signature": None,
                "storage_path": "te/test-id.bin",
                "provides": {},
                "depends": {},
                "created_at": "2026-01-01T00:00:00",
            }]
            resp.error = None
            return resp
        mock_supabase.table.return_value.insert.return_value.execute = fake_execute

        with patch("nff_ota.tools.artifacts.ops.upload_artifact", return_value="te/test-id.bin"):
            result = await upload_artifact(
                UploadArtifactParams(
                    name="fw-esp32", version="1.0.0", device_types=["esp32"],
                    file_path=str(fw_file)
                )
            )

        assert result.error is None
        assert result.artifact is not None
        assert result.artifact.checksum_sha256 == expected_checksum
        assert result.artifact.size_bytes == len(sample_artifact_bytes)

    async def test_upload_missing_file_returns_error(self, mock_supabase):
        result = await upload_artifact(
            UploadArtifactParams(
                name="fw", version="1.0.0", device_types=["esp32"],
                file_path="/nonexistent/path/fw.bin"
            )
        )
        assert result.error is not None
        assert "Cannot read file" in result.error

    async def test_upload_with_signature(self, mock_supabase, sample_artifact_bytes, tmp_path, ed25519_keypair):
        priv_pem, _ = ed25519_keypair
        fw_file = tmp_path / "fw.bin"
        fw_file.write_bytes(sample_artifact_bytes)
        key_file = tmp_path / "sign.pem"
        key_file.write_text(priv_pem)

        captured_row: dict = {}

        def fake_execute():
            resp = MagicMock()
            resp.data = [captured_row] if captured_row else []
            resp.error = None
            return resp

        def fake_insert(row):
            captured_row.update(row)
            m = MagicMock()
            m.execute = fake_execute
            return m

        mock_supabase.table.return_value.insert = fake_insert

        with patch("nff_ota.tools.artifacts.ops.upload_artifact", return_value="xx/id.bin"):
            result = await upload_artifact(
                UploadArtifactParams(
                    name="fw", version="1.0.0", device_types=["esp32"],
                    file_path=str(fw_file), sign_key_path=str(key_file)
                )
            )

        assert result.error is None
        assert captured_row.get("signature") is not None


class TestListArtifacts:
    async def test_empty_list(self, mock_supabase):
        mock_supabase.table.return_value.select.return_value.order.return_value.execute.return_value = MagicMock(data=[], error=None)
        result = await list_artifacts()
        assert result == []

    async def test_filter_by_device_type(self, mock_supabase):
        rows = [
            _make_artifact_row("a1", device_types=["esp32"]),
            _make_artifact_row("a2", device_types=["nrf52"]),
        ]
        mock_supabase.table.return_value.select.return_value.order.return_value.execute.return_value = MagicMock(data=rows, error=None)
        result = await list_artifacts(device_type="esp32")
        assert len(result) == 1
        assert result[0].id == "a1"

    async def test_filter_by_version_prefix(self, mock_supabase):
        rows = [
            _make_artifact_row("a1", version="1.0.0"),
            _make_artifact_row("a2", version="2.0.0"),
        ]
        mock_supabase.table.return_value.select.return_value.order.return_value.execute.return_value = MagicMock(data=rows, error=None)
        result = await list_artifacts(version_prefix="1.")
        assert len(result) == 1
        assert result[0].version == "1.0.0"


class TestDeleteArtifact:
    async def test_delete_success(self, mock_supabase):
        artifact_id = "art-123"
        row = _make_artifact_row(artifact_id)

        call_count = 0

        def table_side_effect(name):
            t = MagicMock()
            t.select.return_value = t
            t.delete.return_value = t
            t.insert.return_value = t
            t.update.return_value = t
            t.eq.return_value = t
            t.in_.return_value = t
            t.order.return_value = t

            if name == "deployments":
                t.execute.return_value = MagicMock(data=[], error=None)
            elif name == "artifacts":
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    t.execute.return_value = MagicMock(data=[row], error=None)
                else:
                    t.execute.return_value = MagicMock(data=[], error=None)
            return t

        mock_supabase.table.side_effect = table_side_effect

        with patch("nff_ota.tools.artifacts.ops.delete_artifact_file", return_value=True):
            result = await delete_artifact(artifact_id)

        assert result.error is None
        assert result.artifact is not None

    async def test_delete_blocked_by_active_deployment(self, mock_supabase):
        artifact_id = "art-123"

        def table_side_effect(name):
            t = MagicMock()
            t.select.return_value = t
            t.delete.return_value = t
            t.eq.return_value = t
            t.in_.return_value = t
            t.order.return_value = t
            if name == "deployments":
                t.execute.return_value = MagicMock(data=[{"id": "dep-1"}], error=None)
            else:
                t.execute.return_value = MagicMock(data=[], error=None)
            return t

        mock_supabase.table.side_effect = table_side_effect
        result = await delete_artifact(artifact_id)
        assert result.error is not None
        assert "active deployment" in result.error

    async def test_delete_not_found(self, mock_supabase):
        def table_side_effect(name):
            t = MagicMock()
            t.select.return_value = t
            t.delete.return_value = t
            t.eq.return_value = t
            t.in_.return_value = t
            t.execute.return_value = MagicMock(data=[], error=None)
            return t

        mock_supabase.table.side_effect = table_side_effect
        result = await delete_artifact("no-such-id")
        assert result.error is not None
        assert "not found" in result.error
