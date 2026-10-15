"""Claim-enrollment fulfillment worker (DEVICE_OWNERSHIP_DESIGN.md §8).

When a member Accepts a device (or a project auto-accepts), the enrollment row goes to state
``accepted`` with ``cert_issued = false``. This worker — not the dashboard click — does the heavy,
rate-limitable work that scales accept-all / auto-accept to 100k devices:

  1. issue a UNIQUE per-device operational cert (signed by the project intermediate),
  2. write the devices row (provision_claimed_device),
  3. sign + publish a ``rollover_cert`` command to the device's bootstrap channel,
  4. on the device's ack, mark the enrollment ``claimed``.

It also runs the rotation reaper: batches past their 24h window are flipped to ``expired`` so a
leaked shared bootstrap credential stops working.
"""

import asyncio
import base64
import json
import logging
import secrets
import time

from cryptography.hazmat.primitives import serialization
from cryptography.x509 import load_pem_x509_certificate

from nff_fleet import db, signing
from nff_fleet.broker import router
from nff_fleet.config import settings
from nff_fleet.provisioning import ca

logger = logging.getLogger(__name__)

_worker_task: asyncio.Task | None = None


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def build_rollover_payload(device_id: str, project_id: str) -> dict:
    """Assemble + sign the rollover_cert command delivering a device its operational identity.

    Reads the just-issued leaf cert/key, the project intermediate, the root CA and the project
    command-verify key. The signature (fleet rollover key) binds project_id + device_id + the leaf
    DER hash, so it can't be replayed onto another device or project (see signing._build_tbs)."""
    device_dir = settings.certs_dir / device_id
    leaf_der = load_pem_x509_certificate((device_dir / "device.crt").read_bytes()).public_bytes(
        serialization.Encoding.DER
    )
    key_der = serialization.load_pem_private_key(
        (device_dir / "device.key").read_bytes(), password=None
    ).private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    intermediate_der = ca.project_ca_cert_der(project_id)
    root_der = load_pem_x509_certificate(settings.tls_ca_cert.read_bytes()).public_bytes(
        serialization.Encoding.DER
    )
    if not root_der:
        raise RuntimeError(
            f"fleet root CA ({settings.tls_ca_cert}) produced an empty DER — refusing to enroll"
        )
    verify_key = signing.public_key_uncompressed(project_id)

    payload = {
        "action": "rollover_cert",
        "nonce": secrets.token_hex(4),
        "timestamp": int(time.time()),
        "project_id": project_id,
        "device_id": device_id,
        "device_cert": _b64(leaf_der),
        "intermediate_cert": _b64(intermediate_der),
        "device_key": _b64(key_der),
        "ca_cert": _b64(root_der),
        "verify_key": _b64(verify_key),
    }
    return signing.sign_rollover(payload)


async def fulfill_one(enrollment: dict) -> bool:
    """Issue a cert + roll it over for one accepted enrollment. Returns True on device ack.

    Reusable by the worker loop and tests. Idempotent enough to retry: a never-acking device is
    marked cert_issued (so it isn't re-issued every sweep) and reconciled to 'claimed' later.
    """
    enrollment_id = enrollment["id"]
    project_id = enrollment["project_id"]
    batch_id = enrollment["batch_id"]
    hwid = enrollment["device_hwid"]
    device_id = hwid  # the hardware id is the stable operational device_id

    signing.ensure_project_key(project_id)
    ca.ensure_project_ca(project_id)
    ca.issue_device_cert(device_id, project_id)
    await db.provision_claimed_device(device_id, project_id)

    payload = build_rollover_payload(device_id, project_id)
    topic = f"nff/_bootstrap/{batch_id}/{hwid}/cmd"

    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    router.register_bootstrap_pending(hwid, fut)
    try:
        await router.publish(topic, json.dumps(payload), qos=1)
        ack = await asyncio.wait_for(fut, timeout=settings.command_timeout)
    except asyncio.TimeoutError:
        logger.warning("rollover not acked by hwid=%s (offline?); marking issued for later reconcile",
                       hwid)
        await db.mark_enrollment_cert_issued(enrollment_id, device_id)
        return False
    except Exception as exc:
        logger.warning("rollover publish/await failed for hwid=%s: %s", hwid, exc)
        return False
    finally:
        router.unregister_bootstrap_pending(hwid)

    if ack.get("status") == "applied" or ack.get("ok") is True:
        await db.mark_enrollment_claimed(enrollment_id, device_id)
        logger.info("Device claimed: hwid=%s device_id=%s project=%s", hwid, device_id, project_id)
        return True

    logger.warning("rollover ack from hwid=%s not successful: %s", hwid, ack)
    await db.mark_enrollment_cert_issued(enrollment_id, device_id)
    return False


async def _worker_loop() -> None:
    try:
        while True:
            await asyncio.sleep(settings.enroll_worker_interval)
            try:
                expired = await db.expire_stale_batches()
                if expired:
                    logger.info("Rotation reaper: %d batch(es) expired (re-flash unclaimed devices)",
                                len(expired))
                # Self-heal: re-open accepted-but-unclaimed enrollments whose rollover was lost
                # (cert_issued=true, never acked) past the grace window, so they are re-rolled below
                # instead of being stranded forever. Grace = a few sweeps so a merely-offline device
                # isn't re-issued a cert every tick.
                reopened = await db.reconcile_stale_enrollments(settings.enroll_reconcile_grace_s)
                if reopened:
                    logger.info("Reconcile: re-opened %d stale enrollment(s) for re-roll", reopened)
                pending = await db.get_accepted_unfulfilled(settings.enroll_worker_batch)
                for e in pending:
                    try:
                        await fulfill_one(e)
                    except Exception as exc:
                        logger.warning("fulfill_one failed for %s: %s", e.get("id"), exc)
            except Exception as exc:
                logger.warning("enroll worker sweep error: %s", exc)
    except asyncio.CancelledError:
        pass


async def start_enroll_worker() -> None:
    global _worker_task
    _worker_task = asyncio.create_task(_worker_loop())
    logger.info("Enroll worker started (interval=%ss, batch=%d)",
                settings.enroll_worker_interval, settings.enroll_worker_batch)


async def stop_enroll_worker() -> None:
    global _worker_task
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None
    logger.info("Enroll worker stopped")
