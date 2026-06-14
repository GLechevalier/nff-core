"""Tests for the OTA-result forwarder and the router dispatch hook.

nff-fleet has no pytest-asyncio config, so async code is driven via asyncio.run().
"""
import asyncio
import json

import nff_fleet.ota_result as otr
import nff_fleet.broker.router as router


class _FakeResp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def _fake_client_factory(captured: dict, status_code: int = 200):
    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResp(status_code)

    return _FakeClient


def test_forward_posts_to_ota(monkeypatch):
    monkeypatch.setattr(otr.settings, "ota_url", "http://localhost:3002")
    monkeypatch.setattr(otr.settings, "mcp_secret", "s3cret")
    captured: dict = {}
    monkeypatch.setattr(otr.httpx, "AsyncClient", _fake_client_factory(captured))

    async def run():
        otr.forward_ota_result(
            "test-device-01",
            {"type": "ota_result", "status": "committed", "version": "2.0.0", "id": "test-device-01"},
        )
        await asyncio.sleep(0.05)  # let the fire-and-forget task run

    asyncio.run(run())
    assert captured["url"] == "http://localhost:3002/internal/ota-result"
    assert captured["headers"]["X-Fleet-Secret"] == "s3cret"
    assert captured["json"] == {"device_id": "test-device-01", "status": "committed", "version": "2.0.0"}


def test_forward_noop_when_url_unset(monkeypatch):
    monkeypatch.setattr(otr.settings, "ota_url", None)
    captured: dict = {}
    monkeypatch.setattr(otr.httpx, "AsyncClient", _fake_client_factory(captured))

    async def run():
        otr.forward_ota_result("test-device-01", {"status": "committed", "version": "2.0.0"})
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert captured == {}


def test_forward_noop_when_fields_missing(monkeypatch):
    monkeypatch.setattr(otr.settings, "ota_url", "http://localhost:3002")
    captured: dict = {}
    monkeypatch.setattr(otr.httpx, "AsyncClient", _fake_client_factory(captured))

    async def run():
        otr.forward_ota_result("test-device-01", {"type": "ota_result"})  # no status/version
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert captured == {}


def test_router_routes_ota_result_to_forwarder(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(router.ota_result, "forward_ota_result",
                        lambda did, pl: calls.__setitem__("fwd", (did, pl)))
    monkeypatch.setattr(router, "resolve_response",
                        lambda did, pl: calls.__setitem__("resolve", (did, pl)))

    payload = json.dumps({"type": "ota_result", "status": "committed", "version": "2.0.0",
                          "id": "test-device-01"}).encode()
    asyncio.run(router._handle_message("nff/project-a/devices/test-device-01/response", payload))

    assert "fwd" in calls and "resolve" not in calls
    assert calls["fwd"][0] == "test-device-01"


def test_router_routes_normal_response_to_resolve(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(router.ota_result, "forward_ota_result",
                        lambda did, pl: calls.__setitem__("fwd", True))
    monkeypatch.setattr(router, "resolve_response",
                        lambda did, pl: calls.__setitem__("resolve", True))

    payload = json.dumps({"type": "pong", "id": "test-device-01"}).encode()
    asyncio.run(router._handle_message("nff/project-a/devices/test-device-01/response", payload))

    assert "resolve" in calls and "fwd" not in calls
