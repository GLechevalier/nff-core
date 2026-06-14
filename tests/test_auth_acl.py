"""Tests for cert→identity binding (DEVICE_OWNERSHIP_DESIGN.md §3) and the topic ACL (§7).

Covers:
  - the signed project_id cert extension round-trips (ca.issue_device_cert / project_id_from_cert)
  - NffCertAuthPlugin derives (project_id, device_id) from the verified peer cert, rejects a
    client_id that doesn't match the cert CN, and trusts the non-TLS internal listener
  - NffTopicAclPlugin scopes a device to its own nff/{project}/devices/{device}/# namespace

nff-fleet has no pytest-asyncio config, so async hooks are driven via asyncio.run().
"""
import asyncio
import logging
import types

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from nff_fleet.broker.acl import NffTopicAclPlugin
from nff_fleet.broker.auth import INTERNAL, NffCertAuthPlugin, session_identity
from nff_fleet.provisioning import ca

DEV = "dev-1"
PROJ = "project-a"


# --------------------------------------------------------------------------- helpers

def _ctx():
    return types.SimpleNamespace(config=None, logger=logging.getLogger("test"))


class _FakeSSL:
    def __init__(self, der: bytes | None):
        self._der = der

    def getpeercert(self, binary_form: bool = False):
        return self._der if binary_form else {}


class _FakeSession:
    def __init__(self, client_id=None, ssl_object=None):
        self.client_id = client_id
        self.ssl_object = ssl_object


@pytest.fixture
def fleet_ca(tmp_path, monkeypatch):
    monkeypatch.setattr(ca.settings, "data_dir", tmp_path)
    # Reset the module-level CA cache so we load the freshly generated CA.
    monkeypatch.setattr(ca, "_ca_key", None)
    monkeypatch.setattr(ca, "_ca_cert", None)
    ca.generate_ca()
    return tmp_path


def _device_cert_der(device_id: str, project_id: str) -> bytes:
    cert_path, _ = ca.issue_device_cert(device_id, project_id)
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return cert.public_bytes(serialization.Encoding.DER)


# --------------------------------------------------------------------------- cert extension

def test_cert_carries_project_id(fleet_ca):
    der = _device_cert_der(DEV, PROJ)
    cert = x509.load_der_x509_certificate(der)
    assert ca.device_id_from_cert(cert) == DEV
    assert ca.project_id_from_cert(cert) == PROJ


# --------------------------------------------------------------------------- auth plugin

def test_auth_accepts_matching_cert(fleet_ca):
    plugin = NffCertAuthPlugin(_ctx())
    session = _FakeSession(client_id=DEV, ssl_object=_FakeSSL(_device_cert_der(DEV, PROJ)))
    assert asyncio.run(plugin.authenticate(session=session)) is True
    assert session_identity(session) == (PROJ, DEV)


def test_auth_rejects_client_id_spoof(fleet_ca):
    # Cert says DEV but the client claims to be someone else → reject (closes T6).
    plugin = NffCertAuthPlugin(_ctx())
    session = _FakeSession(client_id="someone-else", ssl_object=_FakeSSL(_device_cert_der(DEV, PROJ)))
    assert asyncio.run(plugin.authenticate(session=session)) is False
    assert session_identity(session) is None


def test_auth_rejects_no_cert(fleet_ca):
    plugin = NffCertAuthPlugin(_ctx())
    session = _FakeSession(client_id=DEV, ssl_object=_FakeSSL(None))
    assert asyncio.run(plugin.authenticate(session=session)) is False


def test_auth_internal_listener_is_trusted():
    # No TLS (internal router on localhost) → full-access sentinel, no cert needed.
    plugin = NffCertAuthPlugin(_ctx())
    session = _FakeSession(client_id="server", ssl_object=None)
    assert asyncio.run(plugin.authenticate(session=session)) is True
    assert session_identity(session) == (INTERNAL, "server")


# --------------------------------------------------------------------------- topic ACL

def _authed_session(project_id, device_id):
    s = _FakeSession(client_id=device_id)
    s._nff_identity = (project_id, device_id)
    return s


def _filter(plugin, session, topic):
    return asyncio.run(plugin.topic_filtering(session=session, topic=topic, action=None))


def test_acl_allows_own_namespace():
    plugin = NffTopicAclPlugin(_ctx())
    s = _authed_session(PROJ, DEV)
    for t in ("heartbeat", "cmd", "response", "crash"):
        assert _filter(plugin, s, f"nff/{PROJ}/devices/{DEV}/{t}") is True


def test_acl_denies_other_project():
    plugin = NffTopicAclPlugin(_ctx())
    s = _authed_session(PROJ, DEV)
    assert _filter(plugin, s, f"nff/other-project/devices/{DEV}/cmd") is False


def test_acl_denies_other_device():
    plugin = NffTopicAclPlugin(_ctx())
    s = _authed_session(PROJ, DEV)
    assert _filter(plugin, s, f"nff/{PROJ}/devices/dev-2/cmd") is False


def test_acl_denies_unscoped_or_legacy_topic():
    plugin = NffTopicAclPlugin(_ctx())
    s = _authed_session(PROJ, DEV)
    assert _filter(plugin, s, f"nff/devices/{DEV}/cmd") is False  # old flat scheme
    assert _filter(plugin, s, "random/topic") is False


def test_acl_internal_sentinel_allows_all():
    plugin = NffTopicAclPlugin(_ctx())
    s = _FakeSession(client_id="server")
    s._nff_identity = (INTERNAL, "server")
    assert _filter(plugin, s, f"nff/{PROJ}/devices/{DEV}/cmd") is True
    assert _filter(plugin, s, "nff/another/devices/x/heartbeat") is True


def test_acl_denies_unauthenticated_session():
    plugin = NffTopicAclPlugin(_ctx())
    s = _FakeSession(client_id=DEV)  # no _nff_identity stashed
    assert _filter(plugin, s, f"nff/{PROJ}/devices/{DEV}/cmd") is False
