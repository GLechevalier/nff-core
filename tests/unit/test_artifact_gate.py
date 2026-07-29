"""ESP32 image-validity gate at artifact upload + the mock-only group classifier.

The gate rejects anything that isn't a plausible ESP32 image (magic 0xE9 + size floor)
unless the caller proved a synthetic-safe destination (``allow_synthetic=True``). The
classifier decides that by provenance (``mock-`` device ids), not device_type — L1 mocks
and real hardware both self-attest "esp32".
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from nff_ota.config import settings
from nff_ota.models.artifact import UploadArtifactParams
from nff_ota.tools.artifacts.ops import upload_artifact, upload_artifact_impl
from nff_ota.tools.deployments.ops import group_is_mock_only

_VALID_IMAGE = b"\xe9" + b"\x00" * (64 * 1024)


def _params(path: str) -> UploadArtifactParams:
    return UploadArtifactParams(
        name="fw", version="1.0.0", device_types=["esp32"], file_path=path
    )


def _write(tmp_path, data: bytes) -> str:
    f = tmp_path / "artifact.bin"
    f.write_bytes(data)
    return str(f)


class TestImageGate:
    async def test_text_blob_rejected(self, mock_supabase, tmp_path):
        result = await upload_artifact(_params(_write(tmp_path, b"nff-builder feature x\n")))
        assert result.error is not None
        assert "not a valid ESP32 firmware image" in result.error
        assert "0x6e" in result.error  # first byte of the blob, for diagnosis

    async def test_valid_image_passes(self, mock_supabase, tmp_path):
        result = await upload_artifact(_params(_write(tmp_path, _VALID_IMAGE)))
        assert result.error is None

    async def test_tiny_0xe9_blob_rejected(self, mock_supabase, tmp_path):
        result = await upload_artifact(_params(_write(tmp_path, b"\xe9" + b"\x00" * 100)))
        assert result.error is not None
        assert "not a valid ESP32 firmware image" in result.error

    async def test_empty_file_rejected(self, mock_supabase, tmp_path):
        result = await upload_artifact(_params(_write(tmp_path, b"")))
        assert result.error is not None
        assert "empty" in result.error

    async def test_allow_synthetic_bypasses_gate(self, mock_supabase, tmp_path):
        result = await upload_artifact_impl(
            _params(_write(tmp_path, b'{"version": "1.0.0"}')), allow_synthetic=True
        )
        assert result.error is None

    async def test_gate_disabled_passes_anything(self, mock_supabase, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "esp_image_gate_enabled", False)
        result = await upload_artifact(_params(_write(tmp_path, b"anything")))
        assert result.error is None


def _wire_group(mock_supabase, devices: list[dict]) -> None:
    """Serve group members + device rows from the mock supabase."""

    def table_side_effect(name):
        t = MagicMock()
        t.select.return_value = t
        t.eq.return_value = t
        t.in_.return_value = t
        if name == "device_group_members":
            rows = [{"device_id": d["id"]} for d in devices]
        elif name == "devices":
            rows = devices
        else:
            rows = []
        t.execute = AsyncMock(return_value=MagicMock(data=rows, error=None))
        return t

    mock_supabase.table.side_effect = table_side_effect


class TestGroupIsMockOnly:
    async def test_mock_only_group(self, mock_supabase):
        _wire_group(mock_supabase, [{"id": "mock-proj1-abc", "device_type": "esp32"}])
        assert await group_is_mock_only("grp-1") is True

    async def test_real_member_disqualifies(self, mock_supabase):
        _wire_group(
            mock_supabase,
            [{"id": "mock-proj1-abc", "device_type": "esp32"},
             {"id": "esp32-kitchen-01", "device_type": "esp32"}],
        )
        assert await group_is_mock_only("grp-1") is False

    async def test_qemu_member_disqualifies(self, mock_supabase):
        # QEMU boots the bytes even though its id is mock-minted.
        _wire_group(mock_supabase, [{"id": "mock-proj1-q1", "device_type": "nff-qemu-esp32"}])
        assert await group_is_mock_only("grp-1") is False

    async def test_empty_group_is_not_mock_only(self, mock_supabase):
        _wire_group(mock_supabase, [])
        assert await group_is_mock_only("grp-1") is False
