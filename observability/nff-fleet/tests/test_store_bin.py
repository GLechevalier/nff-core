"""Tests for POST /internal/store-bin (the Stage-5 merged-flash-image staging route).

Mirrors test_store_elf.py: the L2 supervisor reads this image back off the shared /data mount to
seed a fresh emulated device's flash. nff-fleet has no pytest-asyncio config, so async handlers are
driven via asyncio.run().
"""
import asyncio
import base64
import json

from nff_fleet import control
from nff_fleet import ota as ota_module
from nff_fleet.config import settings


class FakeRequest:
    def __init__(self, headers=None, body=b""):
        self.headers = headers or {}
        self._body = body

    async def json(self):
        return json.loads(self._body)


def test_store_bin_requires_secret(monkeypatch):
    monkeypatch.setattr(settings, "mcp_secret", "fs")
    req = FakeRequest(headers={}, body=json.dumps({"build_id": "a", "bin_b64": "x"}).encode())
    resp = asyncio.run(control._store_bin(req))
    assert resp.status_code == 401


def test_store_bin_writes_file_keyed_by_build_id(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "mcp_secret", "fs")
    monkeypatch.setattr(settings, "data_dir", tmp_path)  # firmware_dir = data_dir / "firmware"
    bid = "cafe1234cafe1234"
    merged = b"\xe9\x00merged-flash-image"
    body = json.dumps({"build_id": bid, "bin_b64": base64.b64encode(merged).decode()}).encode()
    resp = asyncio.run(control._store_bin(FakeRequest(headers={"x-fleet-secret": "fs"}, body=body)))

    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["build_id"] == bid
    assert data["size_bytes"] == len(merged)
    # Stored where the controller points NFF_MOCK_FIRMWARE_IMAGE and the supervisor reads it.
    assert ota_module.bin_path(bid).read_bytes() == merged


def test_store_bin_rejects_path_traversal_build_id(monkeypatch):
    monkeypatch.setattr(settings, "mcp_secret", "fs")
    body = json.dumps({"build_id": "../etc/passwd", "bin_b64": "x"}).encode()
    resp = asyncio.run(control._store_bin(FakeRequest(headers={"x-fleet-secret": "fs"}, body=body)))
    assert resp.status_code == 400


def test_store_bin_missing_bin_400(monkeypatch):
    monkeypatch.setattr(settings, "mcp_secret", "fs")
    body = json.dumps({"build_id": "abc"}).encode()
    resp = asyncio.run(control._store_bin(FakeRequest(headers={"x-fleet-secret": "fs"}, body=body)))
    assert resp.status_code == 400
