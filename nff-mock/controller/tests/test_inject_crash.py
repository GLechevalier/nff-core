"""POST /mocks/inject-crash — builds a prerecorded crash from the shared scenario catalog and
forwards it to fleet's /internal/inject-crash. The fleet call is stubbed (no network)."""

from __future__ import annotations

import httpx
import pytest

from nff_mock_controller import provision, server
from nff_mock_controller.config import settings

_AUTH = {"X-Control-Secret": "test-secret"}


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(settings, "control_secret", "test-secret")


@pytest.fixture
def captured(monkeypatch):
    """Stub provision._post so the route doesn't hit a real fleet; capture what it forwards."""
    calls: list[tuple[str, dict]] = []

    async def _fake_post(path, body):
        calls.append((path, body))
        return {"device_id": body["device_id"], "injected": True}

    monkeypatch.setattr(provision, "_post", _fake_post)
    return calls


async def _post(path, body, headers=_AUTH):
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ctl") as client:
        return await client.post(path, json=body, headers=headers)


async def test_inject_crash_builds_and_forwards(captured):
    resp = await _post("/mocks/inject-crash", {
        "device_id": "mock-p1-abc", "project_id": "proj-1",
        "scenario": "null_deref", "build_id": "a" * 16, "fw_version": "5.1.0",
    })
    assert resp.status_code == 200
    assert resp.json() == {"device_id": "mock-p1-abc", "scenario": "null_deref", "injected": True}

    assert len(captured) == 1
    path, body = captured[0]
    assert path == "/internal/inject-crash"
    assert body["device_id"] == "mock-p1-abc"
    assert body["project_id"] == "proj-1"
    payload = body["payload"]
    # The forwarded payload is the full crash wire shape built from the catalog.
    assert payload["reason"] == "StoreProhibited"      # null_deref's reason
    assert payload["build_id"] == "a" * 16
    assert payload["fw_version"] == "5.1.0"
    assert payload["backtrace"] and "raw" in payload


async def test_inject_crash_requires_secret(captured):
    resp = await _post("/mocks/inject-crash", {
        "device_id": "mock-x", "project_id": "p", "scenario": "null_deref",
        "build_id": "b" * 16, "fw_version": "1.0.0",
    }, headers={})
    assert resp.status_code == 401
    assert captured == []


async def test_inject_crash_validates_required_fields(captured):
    # missing fw_version
    resp = await _post("/mocks/inject-crash", {
        "device_id": "mock-x", "project_id": "p", "scenario": "null_deref", "build_id": "b" * 16,
    })
    assert resp.status_code == 400
    # missing build_id
    resp = await _post("/mocks/inject-crash", {
        "device_id": "mock-x", "project_id": "p", "scenario": "null_deref", "fw_version": "1.0.0",
    })
    assert resp.status_code == 400
    assert captured == []


async def test_inject_crash_unknown_scenario_falls_back(captured):
    """An unknown scenario resolves to the catalog default rather than erroring."""
    resp = await _post("/mocks/inject-crash", {
        "device_id": "mock-x", "project_id": "p", "scenario": "does-not-exist",
        "build_id": "c" * 16, "fw_version": "2.0.0",
    })
    assert resp.status_code == 200
    # scenarios.resolve fell back to DEFAULT_SCENARIO (transient_load).
    assert resp.json()["scenario"] == "transient_load"
