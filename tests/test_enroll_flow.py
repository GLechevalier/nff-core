"""Enrollment fulfillment: the rollover_cert payload + fulfill_one (DEVICE_OWNERSHIP_DESIGN.md §8).

No real broker/DB: router.publish and the db wrappers are monkeypatched. The rollover signature is
verified against the fleet rollover key the device would pin.
"""
import asyncio
import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from nff_fleet import signing
from nff_fleet import enroll_worker
from nff_fleet.broker import router
from nff_fleet.provisioning import ca

PROJ = "proj-enr-1"
BATCH = "batch-enr"
HWID = "001122334455"


@pytest.fixture
def fleet_ca(tmp_path, monkeypatch):
    monkeypatch.setattr(ca.settings, "data_dir", tmp_path)
    monkeypatch.setattr(ca.settings, "tls_ca_cert", tmp_path / "certs" / "ca.crt")
    ca.clear_cache()
    signing.clear_cache()
    ca.generate_ca()
    yield tmp_path
    ca.clear_cache()
    signing.clear_cache()


def test_rollover_payload_shape_and_signature(fleet_ca):
    signing.ensure_project_key(PROJ)
    ca.issue_device_cert(HWID, PROJ)
    payload = enroll_worker.build_rollover_payload(HWID, PROJ)

    assert payload["action"] == "rollover_cert"
    for f in ("device_cert", "intermediate_cert", "device_key", "ca_cert", "verify_key", "cmd_sig",
              "nonce", "timestamp", "project_id", "device_id"):
        assert f in payload, f
    # base64 blobs decode.
    for f in ("device_cert", "intermediate_cert", "device_key", "ca_cert", "verify_key"):
        assert base64.b64decode(payload[f])

    # The signature verifies against the fleet rollover-verify key (what the device pins).
    pub = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), signing.rollover_verify_key_uncompressed()
    )
    tbs = signing._build_tbs(payload).encode()
    pub.verify(bytes.fromhex(payload["cmd_sig"]), tbs, ec.ECDSA(hashes.SHA256()))  # raises on failure


def test_fulfill_one_issues_cert_publishes_rollover_and_claims(fleet_ca, monkeypatch):
    signing.ensure_project_key(PROJ)
    calls = {"provision": [], "claimed": [], "published": []}

    async def fake_provision(device_id, project_id):
        calls["provision"].append((device_id, project_id))

    async def fake_claimed(enrollment_id, device_id):
        calls["claimed"].append((enrollment_id, device_id))

    async def fake_publish(topic, payload, qos=0):
        calls["published"].append((topic, json.loads(payload)))
        # Simulate the device acking the rollover on its bootstrap response channel.
        hwid = topic.split("/")[3]
        router.resolve_bootstrap_response(hwid, {"type": "rollover_ack", "status": "applied"})

    monkeypatch.setattr(enroll_worker.db, "provision_claimed_device", fake_provision)
    monkeypatch.setattr(enroll_worker.db, "mark_enrollment_claimed", fake_claimed)
    monkeypatch.setattr(router, "publish", fake_publish)

    enrollment = {"id": "enr-1", "project_id": PROJ, "batch_id": BATCH, "device_hwid": HWID}
    acked = asyncio.run(enroll_worker.fulfill_one(enrollment))

    assert acked is True
    assert calls["provision"] == [(HWID, PROJ)]
    assert calls["claimed"] == [("enr-1", HWID)]
    topic, sent = calls["published"][0]
    assert topic == f"nff/_bootstrap/{BATCH}/{HWID}/cmd"
    assert sent["action"] == "rollover_cert" and sent["device_id"] == HWID
    # The operational cert was actually issued.
    assert (ca.settings.certs_dir / HWID / "device.crt").exists()


def test_fulfill_one_timeout_marks_cert_issued(fleet_ca, monkeypatch):
    signing.ensure_project_key(PROJ)
    monkeypatch.setattr(ca.settings, "command_timeout", 0.2)
    issued = []

    async def fake_provision(device_id, project_id):
        pass

    async def fake_cert_issued(enrollment_id, device_id):
        issued.append((enrollment_id, device_id))

    async def fake_publish(topic, payload, qos=0):
        pass  # device never acks

    monkeypatch.setattr(enroll_worker.db, "provision_claimed_device", fake_provision)
    monkeypatch.setattr(enroll_worker.db, "mark_enrollment_cert_issued", fake_cert_issued)
    monkeypatch.setattr(router, "publish", fake_publish)

    enrollment = {"id": "enr-2", "project_id": PROJ, "batch_id": BATCH, "device_hwid": HWID}
    acked = asyncio.run(enroll_worker.fulfill_one(enrollment))

    assert acked is False
    assert issued == [("enr-2", HWID)]


class _FakeRequest:
    def __init__(self, body, secret):
        self._body = body
        self.headers = {"x-fleet-secret": secret}

    async def json(self):
        return self._body


def test_provision_batch_endpoint(fleet_ca, monkeypatch):
    from nff_fleet import control

    monkeypatch.setattr(control.settings, "mcp_secret", "s3cret")

    async def fake_create_batch(project_id, max_devices, bootstrap_serial):
        return "batch-from-db"

    monkeypatch.setattr(control.db, "create_enrollment_batch", fake_create_batch)

    req = _FakeRequest({"project_id": PROJ, "count": 20000}, "s3cret")
    resp = asyncio.run(control._provision_batch(req))
    import json as _json

    data = _json.loads(resp.body)
    assert resp.status_code == 200
    assert data["batch_id"] == "batch-from-db"
    assert data["project_id"] == PROJ
    assert "NFF_BOOTSTRAP 1" in data["bootstrap_header"]
    assert f'NFF_BATCH_ID "batch-from-db"' in data["bootstrap_header"]

    # Wrong secret → 401, no batch created.
    bad = asyncio.run(control._provision_batch(_FakeRequest({"project_id": PROJ}, "nope")))
    assert bad.status_code == 401
