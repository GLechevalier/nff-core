"""Tests for the nff-ota → nff-github compile client (the streaming NDJSON reader).

These prove the client survives the hop that used to be dropped mid-compile: it consumes the
keepalive lines nff-github emits during the build, returns the terminal result line, and maps a
failure line / non-200 to a GithubError — all without needing a real nff-github.
"""
import base64
import json

import httpx
import pytest

from nff_ota import github
from nff_ota.config import settings


def _mock_httpx(monkeypatch, handler):
    """Route the client's httpx.AsyncClient through a MockTransport running ``handler``."""
    transport = httpx.MockTransport(handler)
    real_client_cls = httpx.AsyncClient  # capture before patching to avoid factory recursion

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client_cls(transport=transport)

    monkeypatch.setattr(github.httpx, "AsyncClient", factory)


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(settings, "github_url", "http://nff-github:3003")
    monkeypatch.setattr(settings, "github_secret", "s3cret")


async def test_compile_source_consumes_keepalives_and_returns_result(monkeypatch):
    art = base64.b64encode(b"APP").decode()
    ndjson = (
        json.dumps({"keepalive": True}) + "\n"
        + json.dumps({"keepalive": True}) + "\n"
        + json.dumps({"ok": True, "artifact_b64": art, "elf_b64": None,
                      "build_id": "b16", "sha256": "s", "size_bytes": 3, "logs": ""}) + "\n"
    ).encode()

    def handler(request):
        assert request.headers["x-fleet-secret"] == "s3cret"
        return httpx.Response(200, content=ndjson, headers={"content-type": "application/x-ndjson"})

    _mock_httpx(monkeypatch, handler)
    data = await github.compile_source("void setup(){}", "b16", "2.0.0", "nff-qemu-esp32")
    assert data["ok"] is True
    assert base64.b64decode(data["artifact_b64"]) == b"APP"
    assert data["build_id"] == "b16"


async def test_compile_source_maps_failure_line_to_error(monkeypatch):
    ndjson = (
        json.dumps({"keepalive": True}) + "\n"
        + json.dumps({"ok": False, "error": "arduino-cli compile failed", "logs": "..."}) + "\n"
    ).encode()

    def handler(request):
        return httpx.Response(200, content=ndjson, headers={"content-type": "application/x-ndjson"})

    _mock_httpx(monkeypatch, handler)
    with pytest.raises(github.GithubError, match="compile failed"):
        await github.compile_source("bad", "b16", "2.0.0", "nff-qemu-esp32")


async def test_compile_source_maps_non_200_to_error(monkeypatch):
    def handler(request):
        return httpx.Response(401, json={"error": "unauthorized"})

    _mock_httpx(monkeypatch, handler)
    with pytest.raises(github.GithubError):
        await github.compile_source("x", "b16", "2.0.0", "nff-qemu-esp32")


async def test_compile_source_errors_on_no_result(monkeypatch):
    # Only keepalives, no terminal line (e.g. the stream was cut) → an explicit error.
    ndjson = (json.dumps({"keepalive": True}) + "\n").encode()

    def handler(request):
        return httpx.Response(200, content=ndjson, headers={"content-type": "application/x-ndjson"})

    _mock_httpx(monkeypatch, handler)
    with pytest.raises(github.GithubError, match="no result"):
        await github.compile_source("x", "b16", "2.0.0", "nff-qemu-esp32")
