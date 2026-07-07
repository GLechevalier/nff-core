"""Tests for POST /internal/inject-crash (the instant-crash demo injection route).

A real L2 QEMU device has no `crash` command, so the demo injects a prerecorded crash: the
route stamps the topic-authoritative project_id, caches it (so fleet_get_crash returns it), and
writes the durable crash_reports row via db.ingest_crash.

nff-fleet has no pytest-asyncio config, so async handlers are driven via asyncio.run().
"""
import asyncio
import json

from nff_fleet import control, db
from nff_fleet.broker import router
from nff_fleet.config import settings


class FakeRequest:
    def __init__(self, headers=None, body=b""):
        self.headers = headers or {}
        self._body = body

    async def json(self):
        return json.loads(self._body)


def _body(**kw) -> bytes:
    return json.dumps(kw).encode()


def test_inject_crash_requires_secret(monkeypatch):
    monkeypatch.setattr(settings, "mcp_secret", "fs")
    req = FakeRequest(headers={}, body=_body(device_id="mock-x", project_id="p", payload={}))
    resp = asyncio.run(control._inject_crash(req))
    assert resp.status_code == 401


def test_inject_crash_caches_and_ingests(monkeypatch):
    monkeypatch.setattr(settings, "mcp_secret", "fs")
    router._crash_cache.clear()
    ingested = {}
    monkeypatch.setattr(db, "ingest_crash", lambda dev, payload: ingested.update(dev=dev, payload=payload))

    payload = {"reason": "StoreProhibited", "crash_count": 2, "backtrace": [{"address": "0x1", "resolved": "f"}]}
    body = _body(device_id="mock-abc", project_id="proj-1", payload=payload)
    resp = asyncio.run(control._inject_crash(FakeRequest(headers={"x-fleet-secret": "fs"}, body=body)))

    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data == {"device_id": "mock-abc", "injected": True}

    # fleet_get_crash reads the cache: project_id is stamped authoritatively from the request.
    cached = router.get_crash("mock-abc")
    assert cached["reason"] == "StoreProhibited"
    assert cached["project_id"] == "proj-1"
    # The durable crash_reports row was written with the same (project-stamped) payload.
    assert ingested["dev"] == "mock-abc"
    assert ingested["payload"]["project_id"] == "proj-1"


def test_inject_crash_requires_valid_ids(monkeypatch):
    monkeypatch.setattr(settings, "mcp_secret", "fs")
    # bad device_id
    resp = asyncio.run(control._inject_crash(
        FakeRequest(headers={"x-fleet-secret": "fs"}, body=_body(device_id="", project_id="p", payload={}))
    ))
    assert resp.status_code == 400
    # bad project_id
    resp = asyncio.run(control._inject_crash(
        FakeRequest(headers={"x-fleet-secret": "fs"}, body=_body(device_id="mock-x", project_id="", payload={}))
    ))
    assert resp.status_code == 400
    # payload not an object
    resp = asyncio.run(control._inject_crash(
        FakeRequest(headers={"x-fleet-secret": "fs"}, body=_body(device_id="mock-x", project_id="p", payload="nope"))
    ))
    assert resp.status_code == 400


def test_set_crash_roundtrips_through_get_crash():
    router._crash_cache.clear()
    router.set_crash("mock-y", {"reason": "LoadProhibited", "project_id": "p2"})
    assert router.get_crash("mock-y") == {"reason": "LoadProhibited", "project_id": "p2"}
