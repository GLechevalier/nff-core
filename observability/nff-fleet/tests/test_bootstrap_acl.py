"""Bootstrap (shared batch) auth + ACL confinement (DEVICE_OWNERSHIP_DESIGN.md §8).

A bootstrap device presents the shared batch cert (CN=batch:{id}) and sets its own hwid as the
client_id. Auth accepts it into a (BOOTSTRAP_SENTINEL, hwid) identity + stashes the batch_id; the
ACL must confine it to nff/_bootstrap/{batch_id}/{hwid}/# and deny every project namespace.
"""
import asyncio
import logging
import types

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from nff_fleet import signing
from nff_fleet.broker.acl import NffTopicAclPlugin
from nff_fleet.broker.auth import NffCertAuthPlugin, session_batch, session_identity
from nff_fleet.provisioning import ca, batch
from nff_fleet.provisioning.batch import BOOTSTRAP_SENTINEL

PROJ = "proj-boot-1"
BATCH = "batch-xyz"
HWID = "aabbccddeeff"


@pytest.fixture
def fleet_ca(tmp_path, monkeypatch):
    monkeypatch.setattr(ca.settings, "data_dir", tmp_path)
    ca.clear_cache()
    signing.clear_cache()
    ca.generate_ca()
    yield tmp_path
    ca.clear_cache()
    signing.clear_cache()


def _ctx():
    return types.SimpleNamespace(config=None, logger=logging.getLogger("test"))


class _FakeSSL:
    def __init__(self, der):
        self._der = der

    def getpeercert(self, binary_form=False):
        return self._der if binary_form else {}


class _FakeSession:
    def __init__(self, client_id=None, ssl_object=None):
        self.client_id = client_id
        self.ssl_object = ssl_object


def _batch_der():
    cert_path, _ = batch.issue_batch_cert(PROJ, BATCH)
    return x509.load_pem_x509_certificate(cert_path.read_bytes()).public_bytes(serialization.Encoding.DER)


def _filter(plugin, session, topic):
    return asyncio.run(plugin.topic_filtering(session=session, topic=topic, action=None))


# --------------------------------------------------------------------------- auth

def test_auth_accepts_batch_cert_as_bootstrap(fleet_ca):
    plugin = NffCertAuthPlugin(_ctx())
    s = _FakeSession(client_id=HWID, ssl_object=_FakeSSL(_batch_der()))
    assert asyncio.run(plugin.authenticate(session=s)) is True
    assert session_identity(s) == (BOOTSTRAP_SENTINEL, HWID)
    assert session_batch(s) == BATCH


def test_auth_batch_cert_requires_client_id(fleet_ca):
    plugin = NffCertAuthPlugin(_ctx())
    s = _FakeSession(client_id=None, ssl_object=_FakeSSL(_batch_der()))
    assert asyncio.run(plugin.authenticate(session=s)) is False


# --------------------------------------------------------------------------- acl

def _boot_session():
    s = _FakeSession(client_id=HWID)
    s._nff_identity = (BOOTSTRAP_SENTINEL, HWID)
    s._nff_batch = BATCH
    return s


def test_acl_allows_only_own_bootstrap_namespace(fleet_ca):
    plugin = NffTopicAclPlugin(_ctx())
    s = _boot_session()
    for t in ("announce", "cmd", "response"):
        assert _filter(plugin, s, f"nff/_bootstrap/{BATCH}/{HWID}/{t}") is True


def test_acl_denies_other_batch_or_hwid(fleet_ca):
    plugin = NffTopicAclPlugin(_ctx())
    s = _boot_session()
    assert _filter(plugin, s, f"nff/_bootstrap/other-batch/{HWID}/announce") is False
    assert _filter(plugin, s, f"nff/_bootstrap/{BATCH}/other-hwid/announce") is False


def test_acl_denies_bootstrap_device_project_topics(fleet_ca):
    plugin = NffTopicAclPlugin(_ctx())
    s = _boot_session()
    # The whole point: a shared-cred device can never touch a real project namespace.
    assert _filter(plugin, s, f"nff/{PROJ}/devices/{HWID}/cmd") is False
    assert _filter(plugin, s, f"nff/{PROJ}/devices/{HWID}/heartbeat") is False


def test_acl_denies_operational_device_bootstrap_ns(fleet_ca):
    plugin = NffTopicAclPlugin(_ctx())
    s = _FakeSession(client_id="dev-9")
    s._nff_identity = (PROJ, "dev-9")  # operational identity
    assert _filter(plugin, s, f"nff/_bootstrap/{BATCH}/dev-9/cmd") is False
