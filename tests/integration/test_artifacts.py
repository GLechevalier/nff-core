"""Integration tests for artifact tools using a mocked Supabase client.

Artifacts are stored on the shared firmware_versions table, keyed by version.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nff_ota.crypto import sha256_bytes
from nff_ota.models.artifact import UploadArtifactParams
from nff_ota.tools.artifacts.ops import delete_artifact, list_artifacts, upload_artifact


def _make_fw_row(
    version: str = "1.0.0",
    name: str = "fw",
    device_types: list[str] | None = None,
    checksum: str = "abc",
    storage_path: str | None = None,
) -> dict:
    return {
        "version": version,
        "name": name,
        "device_types": device_types or ["esp32"],
        "checksum_sha256": checksum,
        "size_bytes": 1024,
        "signature": None,
        "storage_path": storage_path or f"firmware/{version}.bin",
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

        row = _make_fw_row(version="1.0.0", name="fw-esp32", checksum=expected_checksum)
        row["size_bytes"] = len(sample_artifact_bytes)
        mock_supabase.table.return_value.upsert.return_value.execute = AsyncMock(
            return_value=MagicMock(data=[row], error=None)
        )

        result = await upload_artifact(
            UploadArtifactParams(
                name="fw-esp32", version="1.0.0", device_types=["esp32"],
                file_path=str(fw_file)
            )
        )

        assert result.error is None
        assert result.artifact is not None
        assert result.artifact.id == "1.0.0"
        assert result.artifact.checksum_sha256 == expected_checksum
        assert result.artifact.size_bytes == len(sample_artifact_bytes)

    async def test_upload_writes_lineage_when_provided(
        self, mock_supabase, sample_artifact_bytes, tmp_path
    ):
        """build_id + git lineage land in the upserted row; parent_version is linked
        only because the (mocked) parent row exists."""
        fw_file = tmp_path / "fw.bin"
        fw_file.write_bytes(sample_artifact_bytes)

        row = _make_fw_row(version="hello-e20bdf5", name="Fix drift")
        row["build_id"] = "e20bdf5000000000"
        # Shared execute mock: the parent-existence SELECT and the upsert both see this,
        # so the parent is treated as existing and the link is kept.
        mock_supabase.table.return_value.execute = AsyncMock(
            return_value=MagicMock(data=[row], error=None)
        )

        result = await upload_artifact(
            UploadArtifactParams(
                name="Fix drift", version="hello-e20bdf5", device_types=["esp32"],
                file_path=str(fw_file),
                build_id="e20bdf5000000000", branch_name="main", author="octocat",
                committed_at="2026-06-09T07:26:55Z", changelog="Fix drift",
                parent_version="hello-0000000", elf_path="firmware/hello-e20bdf5.elf",
            )
        )

        assert result.error is None
        written = mock_supabase.table.return_value.upsert.call_args.args[0]
        assert written["build_id"] == "e20bdf5000000000"
        assert written["branch_name"] == "main"
        assert written["author"] == "octocat"
        assert written["committed_at"] == "2026-06-09T07:26:55Z"
        assert written["changelog"] == "Fix drift"
        assert written["parent_version"] == "hello-0000000"
        assert written["elf_path"] == "firmware/hello-e20bdf5.elf"
        assert written["elf_stored"] is True

    async def test_upload_omits_parent_version_when_parent_absent(
        self, mock_supabase, sample_artifact_bytes, tmp_path
    ):
        """A parent_version whose row does not exist is dropped, not written (FK safety)."""
        fw_file = tmp_path / "fw.bin"
        fw_file.write_bytes(sample_artifact_bytes)
        # Default mock: every execute() returns data=[] -> parent lookup finds nothing.
        result = await upload_artifact(
            UploadArtifactParams(
                name="v", version="hello-e20bdf5", device_types=["esp32"],
                file_path=str(fw_file), build_id="e20bdf5000000000",
                parent_version="hello-0000000",
            )
        )
        assert result.error is None
        written = mock_supabase.table.return_value.upsert.call_args.args[0]
        assert "parent_version" not in written
        assert written["build_id"] == "e20bdf5000000000"

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

        async def fake_execute():
            resp = MagicMock()
            resp.data = [captured_row] if captured_row else []
            resp.error = None
            return resp

        def fake_upsert(row, **kwargs):
            captured_row.update(row)
            m = MagicMock()
            m.execute = fake_execute
            return m

        mock_supabase.table.return_value.upsert = fake_upsert

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
        mock_supabase.table.return_value.select.return_value.order.return_value.execute = AsyncMock(
            return_value=MagicMock(data=[], error=None)
        )
        result = await list_artifacts()
        assert result == []

    async def test_filter_by_device_type(self, mock_supabase):
        rows = [
            _make_fw_row("1.0.0", device_types=["esp32"]),
            _make_fw_row("1.1.0", device_types=["nrf52"]),
        ]
        mock_supabase.table.return_value.select.return_value.order.return_value.execute = AsyncMock(
            return_value=MagicMock(data=rows, error=None)
        )
        result = await list_artifacts(device_type="esp32")
        assert len(result) == 1
        assert result[0].id == "1.0.0"

    async def test_filter_by_version_prefix(self, mock_supabase):
        rows = [
            _make_fw_row("1.0.0"),
            _make_fw_row("2.0.0"),
        ]
        mock_supabase.table.return_value.select.return_value.order.return_value.execute = AsyncMock(
            return_value=MagicMock(data=rows, error=None)
        )
        result = await list_artifacts(version_prefix="1.")
        assert len(result) == 1
        assert result[0].version == "1.0.0"

    async def test_skips_rows_without_binary(self, mock_supabase):
        rows = [
            _make_fw_row("1.0.0"),                       # has storage_path
            {**_make_fw_row("0.9.0"), "storage_path": None},  # lineage-only row
        ]
        mock_supabase.table.return_value.select.return_value.order.return_value.execute = AsyncMock(
            return_value=MagicMock(data=rows, error=None)
        )
        result = await list_artifacts()
        assert len(result) == 1
        assert result[0].version == "1.0.0"


class TestDeleteArtifact:
    async def test_delete_success(self, mock_supabase):
        version = "1.0.0"
        row = _make_fw_row(version)
        call_count = [0]

        def table_side_effect(name):
            t = MagicMock()
            t.select.return_value = t
            t.delete.return_value = t
            t.insert.return_value = t
            t.update.return_value = t
            t.eq.return_value = t
            t.in_.return_value = t
            t.order.return_value = t

            async def execute():
                if name == "deployments":
                    return MagicMock(data=[], error=None)
                call_count[0] += 1
                if call_count[0] == 1:
                    return MagicMock(data=[row], error=None)  # fetch the version
                return MagicMock(data=[], error=None)          # clear update
            t.execute = execute
            return t

        mock_supabase.table.side_effect = table_side_effect

        with patch("nff_ota.tools.artifacts.ops.delete_artifact_file", new=AsyncMock(return_value=True)):
            result = await delete_artifact(version)

        assert result.error is None
        assert result.artifact is not None
        assert result.artifact.version == version

    async def test_delete_blocked_by_active_deployment(self, mock_supabase):
        def table_side_effect(name):
            t = MagicMock()
            t.select.return_value = t
            t.delete.return_value = t
            t.eq.return_value = t
            t.in_.return_value = t
            t.order.return_value = t
            if name == "deployments":
                t.execute = AsyncMock(return_value=MagicMock(data=[{"id": "dep-1"}], error=None))
            else:
                t.execute = AsyncMock(return_value=MagicMock(data=[], error=None))
            return t

        mock_supabase.table.side_effect = table_side_effect
        result = await delete_artifact("1.0.0")
        assert result.error is not None
        assert "active deployment" in result.error

    async def test_delete_not_found(self, mock_supabase):
        def table_side_effect(name):
            t = MagicMock()
            t.select.return_value = t
            t.delete.return_value = t
            t.eq.return_value = t
            t.in_.return_value = t
            t.execute = AsyncMock(return_value=MagicMock(data=[], error=None))
            return t

        mock_supabase.table.side_effect = table_side_effect
        result = await delete_artifact("no-such-version")
        assert result.error is not None
        assert "not found" in result.error
