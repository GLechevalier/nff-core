"""Tests for POST /internal/store-elf (the Stage-4 ELF-staging route).

nff-fleet has no pytest-asyncio config, so async handlers are driven via asyncio.run().
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


def test_store_elf_requires_secret(monkeypatch):
    monkeypatch.setattr(settings, "mcp_secret", "fs")
    req = FakeRequest(headers={}, body=json.dumps({"build_id": "a", "elf_b64": "x"}).encode())
    resp = asyncio.run(control._store_elf(req))
    assert resp.status_code == 401


def test_store_elf_writes_file_keyed_by_build_id(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "mcp_secret", "fs")
    monkeypatch.setattr(settings, "data_dir", tmp_path)  # firmware_dir = data_dir / "firmware"
    bid = "cafe1234cafe1234"
    elf = b"\x7fELFreal-bytes"
    body = json.dumps({"build_id": bid, "elf_b64": base64.b64encode(elf).decode()}).encode()
    resp = asyncio.run(control._store_elf(FakeRequest(headers={"x-fleet-secret": "fs"}, body=body)))

    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["build_id"] == bid
    assert data["size_bytes"] == len(elf)
    # The ELF is stored where fleet_resolve_backtrace will look it up by build_id.
    assert ota_module.elf_path(bid).read_bytes() == elf


def test_store_elf_rejects_path_traversal_build_id(monkeypatch):
    monkeypatch.setattr(settings, "mcp_secret", "fs")
    body = json.dumps({"build_id": "../etc/passwd", "elf_b64": "x"}).encode()
    resp = asyncio.run(control._store_elf(FakeRequest(headers={"x-fleet-secret": "fs"}, body=body)))
    assert resp.status_code == 400


def test_store_elf_missing_elf_400(monkeypatch):
    monkeypatch.setattr(settings, "mcp_secret", "fs")
    body = json.dumps({"build_id": "abc"}).encode()
    resp = asyncio.run(control._store_elf(FakeRequest(headers={"x-fleet-secret": "fs"}, body=body)))
    assert resp.status_code == 400
