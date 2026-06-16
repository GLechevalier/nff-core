from __future__ import annotations

import os
import ssl
import tempfile

from nff_mock.config import NffMockSettings

# MQTT topic schema — project-scoped per the device-ownership design
# (DEVICE_OWNERSHIP_DESIGN.md §7). The broker ACL derives (project_id, device_id)
# from the verified cert and only allows nff/{project_id}/devices/{device_id}/#.
#   nff/{project_id}/devices/{id}/heartbeat   device publishes on connect (retained, QoS 1)
#   nff/{project_id}/devices/{id}/cmd         server publishes signed commands (QoS 1)
#   nff/{project_id}/devices/{id}/response    device publishes command results (QoS 1)
#   nff/{project_id}/devices/{id}/crash       device publishes crash reports on boot (retained)


def topic_heartbeat(project_id: str, device_id: str) -> str:
    return f"nff/{project_id}/devices/{device_id}/heartbeat"


def topic_cmd(project_id: str, device_id: str) -> str:
    return f"nff/{project_id}/devices/{device_id}/cmd"


def topic_response(project_id: str, device_id: str) -> str:
    return f"nff/{project_id}/devices/{device_id}/response"


def topic_crash(project_id: str, device_id: str) -> str:
    return f"nff/{project_id}/devices/{device_id}/crash"


def build_ssl_context(cfg: NffMockSettings) -> ssl.SSLContext:
    """mTLS context: verify the broker against the nff CA, and present this device's
    client cert + key. The fleet server cert has no `nff-fleet` DNS SAN (only the
    broker IP + localhost), so hostname checking must be disabled while still
    verifying the CA chain — connecting by service name would otherwise fail.

    The device leaf chains leaf -> project intermediate -> root, but the broker trusts only the
    root, so we must *present* the project intermediate alongside the leaf (real firmware bakes
    this into credentials.h). Without it the broker can't build the chain and silently drops the
    handshake (TLS 1.3 stalls, TLS 1.2 EOFs), so the device never connects.
    """
    ctx = ssl.create_default_context(
        purpose=ssl.Purpose.SERVER_AUTH, cafile=str(cfg.ca_cert)
    )
    _load_client_chain(ctx, cfg)
    if cfg.tls_insecure_hostname:
        ctx.check_hostname = False
    return ctx


def _load_client_chain(ctx: ssl.SSLContext, cfg: NffMockSettings) -> None:
    """Load the device key + the full client cert chain (leaf + project intermediate).

    ``SSLContext.load_cert_chain`` only takes file paths and reads intermediates from the same
    PEM file as the leaf, so when a project CA is present we concatenate leaf + intermediate into
    a throwaway temp file. Falls back to the leaf alone for untenanted dev (no project CA).
    """
    pca = cfg.project_ca_cert
    if pca is None or not pca.exists():
        ctx.load_cert_chain(certfile=str(cfg.device_cert), keyfile=str(cfg.device_key))
        return
    chain = cfg.device_cert.read_text() + "\n" + pca.read_text()
    tmp = tempfile.NamedTemporaryFile("w", suffix=".crt", delete=False)
    try:
        tmp.write(chain)
        tmp.close()
        # load_cert_chain reads the PEM into OpenSSL immediately, so the temp file is no longer
        # needed once it returns.
        ctx.load_cert_chain(certfile=tmp.name, keyfile=str(cfg.device_key))
    finally:
        os.unlink(tmp.name)
