from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_client: Any | None = None
_enabled: bool = False


async def init() -> None:
    global _client, _enabled
    from nff_fleet.config import settings
    if not settings.supabase_url or not settings.supabase_key:
        logger.info("db: SUPABASE_URL/KEY not configured — persistence disabled")
        return
    from supabase import acreate_client
    _client = await acreate_client(settings.supabase_url, settings.supabase_key)
    _enabled = True
    logger.info("db: Supabase client ready")


async def shutdown() -> None:
    global _client, _enabled
    _client = None
    _enabled = False


def _fire(coro) -> None:
    async def _run():
        try:
            await coro
        except Exception as exc:
            logger.warning("db task error: %s", exc)
    asyncio.create_task(_run())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Device presence
# ---------------------------------------------------------------------------

def upsert_device(device_id: str, payload: dict[str, Any], with_telemetry: bool = False) -> None:
    if not _enabled:
        return
    if with_telemetry:
        _fire(_upsert_device_and_telemetry(device_id, payload))
    else:
        _fire(_upsert_device(device_id, payload))


async def _upsert_device_and_telemetry(device_id: str, payload: dict[str, Any]) -> None:
    await _upsert_device(device_id, payload)
    await _insert_telemetry(device_id, payload)


async def _upsert_device(device_id: str, payload: dict[str, Any]) -> None:
    now = _now()
    # Device-attested hardware identity (see nff-sdk-c heartbeat). device_type is the
    # canonical family used by nff-ota's OTA compatibility gate; hardware is the richer
    # fingerprint kept for diagnostics. Both are omitted by older firmware, in which
    # case the RPC preserves any previously-stored value.
    hardware = {
        k: payload[k]
        for k in ("fqbn", "chip", "rev", "flash")
        if payload.get(k) not in (None, "")
    }
    # The heartbeat (nff-sdk-c) sends "fw" and "uptime" (milliseconds); fall back to the
    # DB-style keys so any non-device caller still works.
    firmware_version = payload.get("fw") or payload.get("firmware_version") or "unknown"
    uptime_seconds = payload.get("uptime_seconds")
    if uptime_seconds is None and payload.get("uptime") is not None:
        uptime_seconds = int(payload["uptime"]) // 1000
    await _client.rpc(
        "upsert_device_heartbeat",
        {
            "p_id": device_id,
            "p_status": payload.get("status", "online"),
            "p_firmware_version": firmware_version,
            "p_ip_address": payload.get("ip"),
            "p_rssi": payload.get("rssi"),
            "p_uptime_seconds": uptime_seconds or 0,
            "p_last_seen": now,
            "p_updated_at": now,
            "p_device_type": payload.get("device_type") or None,
            "p_hardware": hardware,
            # The device-reported build_id (heartbeat "build"); the connector's post-deploy
            # verification compares it against what it pushed. Omitted by older firmware.
            "p_build_id": payload.get("build") or None,
            # Tenancy: mocks echo their project_id in the heartbeat so they land in the
            # right project (real ESP32s omit it; the RPC COALESCE-preserves any known
            # value). See nff-db migration 0029.
            "p_project_id": payload.get("project_id") or None,
            # Device-attested heartbeat frequency; the reaper + dashboard login reconcile
            # mark a device offline after 2× this many seconds. Omitted by older firmware,
            # in which case the RPC COALESCE-preserves any previously-stored value.
            "p_heartbeat_interval_s": payload.get("interval_s"),
            # Optional friendly display name (mocks set NFF_MOCK_NAME). The RPC seeds
            # devices.name on first insert ONLY — never on conflict — so a later heartbeat
            # or a dashboard rename is never clobbered. Omitted by real firmware.
            "p_name": payload.get("name") or None,
        },
    ).execute()


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

def insert_telemetry(device_id: str, payload: dict[str, Any]) -> None:
    if not _enabled:
        return
    _fire(_insert_telemetry(device_id, payload))


async def _insert_telemetry(device_id: str, payload: dict[str, Any]) -> None:
    await _client.table("device_telemetry").insert(
        {
            "device_id": device_id,
            "ts": _now(),
            "rssi": payload.get("rssi"),
            # Heartbeat sends free heap as "heap"; fall back to the DB-style key.
            "heap_free": payload.get("heap", payload.get("heap_free")),
            "packets_rx": payload.get("packets_rx"),
            "packets_tx": payload.get("packets_tx"),
            "temp_c": payload.get("temp_c"),
            # Denormalized tenant scope (nullable since 0029): mocks echo project_id,
            # real/projectless devices send none and the row stays NULL (RLS-hidden).
            "project_id": payload.get("project_id") or None,
        }
    ).execute()


# ---------------------------------------------------------------------------
# Mock cleanup (controller calls fleet /internal/deprovision, which calls this)
# ---------------------------------------------------------------------------

async def delete_mock_device(device_id: str) -> None:
    """Drop a torn-down mock's device row (telemetry/crash/jobs cascade). The RPC
    is constrained to 'mock-'-prefixed ids, so a real device can never be removed.
    No-op when persistence is disabled."""
    if not _enabled:
        return
    await _client.rpc("delete_mock_device", {"p_id": device_id}).execute()


# ---------------------------------------------------------------------------
# Batch claim enrollment (DEVICE_OWNERSHIP_DESIGN.md §8; nff-db migration 0030)
# ---------------------------------------------------------------------------

async def create_enrollment_batch(
    project_id: str, max_devices: int | None, bootstrap_serial: str | None
) -> str | None:
    """Create a batch (24h window via the RPC default). Returns the batch_id, or None if
    persistence is disabled."""
    if not _enabled:
        return None
    resp = await _client.rpc(
        "create_enrollment_batch",
        {
            "p_project_id": project_id,
            "p_max_devices": max_devices,
            "p_bootstrap_serial": bootstrap_serial,
        },
    ).execute()
    return resp.data


async def record_enrollment(batch_id: str, device_hwid: str, hardware: dict[str, Any]) -> str:
    """Record a device announcing under a batch. Returns the resulting state string
    ('pending'|'accepted'|'rejected:expired'|'rejected:quota'). Quota + 24h TTL are
    enforced inside the RPC."""
    if not _enabled:
        return "rejected:disabled"
    resp = await _client.rpc(
        "record_enrollment",
        {"p_batch_id": batch_id, "p_device_hwid": device_hwid, "p_hardware": hardware or {}},
    ).execute()
    return resp.data


async def get_accepted_unfulfilled(limit: int = 100) -> list[dict[str, Any]]:
    """Accepted enrollments still awaiting cert issuance (the fulfillment worker's work list)."""
    if not _enabled:
        return []
    resp = await (
        _client.table("device_enrollments")
        .select("id, batch_id, project_id, device_hwid")
        .eq("state", "accepted")
        .eq("cert_issued", False)
        .limit(limit)
        .execute()
    )
    return resp.data or []


async def get_active_batch(project_id: str) -> str | None:
    """Newest non-expired active batch for a project, or None.

    Lets a repeated onboarding (`nff init` re-run) reuse the existing bootstrap
    credential instead of minting a fresh batch + cert every time. expires_at is
    filtered in the future so we never hand back a batch a device would announce
    into only to be rejected as expired by record_enrollment's 24h TTL check."""
    if not _enabled:
        return None
    now_iso = datetime.now(timezone.utc).isoformat()
    resp = await (
        _client.table("enrollment_batches")
        .select("id")
        .eq("project_id", project_id)
        .eq("state", "active")
        .gt("expires_at", now_iso)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0]["id"] if rows else None


async def provision_claimed_device(device_id: str, project_id: str) -> None:
    """Write the devices row at claim time (ota_status='pending'). Ownership is recorded here,
    not lazily on heartbeat."""
    if not _enabled:
        return
    await _client.rpc(
        "provision_claimed_device",
        {"p_device_id": device_id, "p_project_id": project_id},
    ).execute()


async def mark_enrollment_claimed(enrollment_id: str, device_id: str) -> None:
    """Flag an enrollment claimed once the device acks rollover."""
    if not _enabled:
        return
    await _client.rpc(
        "mark_enrollment_claimed",
        {"p_enrollment_id": enrollment_id, "p_device_id": device_id},
    ).execute()


async def mark_enrollment_cert_issued(enrollment_id: str, device_id: str) -> None:
    """Set cert_issued=true (+ device_id) WITHOUT claiming, so the fulfillment worker stops
    re-issuing a cert for a device that didn't ack rollover within the window. The enrollment is
    reconciled to 'claimed' when the device next connects operationally (or via a manual re-push)."""
    if not _enabled:
        return
    await (
        _client.table("device_enrollments")
        .update({"cert_issued": True, "device_id": device_id})
        .eq("id", enrollment_id)
        .execute()
    )


async def claim_enrollment_by_device(device_id: str) -> int:
    """Finalize any 'accepted' enrollment for this device to 'claimed'. An operational heartbeat
    is proof the device applied its cert — this recovers a claim whose (QoS-0) rollover ack was
    lost, so the enrollment isn't stranded at accepted+cert_issued. Returns rows finalized."""
    if not _enabled:
        return 0
    resp = await _client.rpc(
        "claim_enrollment_by_device", {"p_device_id": device_id}
    ).execute()
    return resp.data or 0


async def reconcile_stale_enrollments(grace_seconds: int = 60) -> int:
    """Re-open accepted-but-unclaimed enrollments whose cert was issued but never acked (rollover
    lost), older than grace_seconds, so the fulfillment worker re-rolls them. Returns rows re-opened."""
    if not _enabled:
        return 0
    resp = await _client.rpc(
        "reconcile_stale_enrollments", {"p_grace_seconds": grace_seconds}
    ).execute()
    return resp.data or 0


async def expire_stale_batches() -> list[str]:
    """Flip active→expired for batches past their 24h window; returns affected batch ids."""
    if not _enabled:
        return []
    resp = await _client.rpc("expire_stale_batches", {}).execute()
    data = resp.data or []
    # setof uuid comes back as a list of scalars or {"expire_stale_batches": uuid} rows.
    out: list[str] = []
    for row in data:
        out.append(row if isinstance(row, str) else row.get("expire_stale_batches") or row.get("id"))
    return [x for x in out if x]


# ---------------------------------------------------------------------------
# Crash ingestion
# ---------------------------------------------------------------------------

def ingest_crash(device_id: str, payload: dict[str, Any]) -> None:
    if not _enabled:
        return
    _fire(_ingest_crash(device_id, payload))


async def _ingest_crash(device_id: str, payload: dict[str, Any]) -> None:
    crash_id = str(uuid.uuid4())

    await _client.table("crash_reports").upsert(
        {
            "id": crash_id,
            "device_id": device_id,
            # Denormalized tenant scope (nullable since 0029): the router stamps the
            # topic-authoritative project_id into the payload, so project-scoped readers
            # (agent-worker senseFleet) see the crash. Mirrors upsert_device / announce.
            "project_id": payload.get("project_id") or None,
            "ts": _now(),
            "reason": payload.get("reason"),
            "exception_cause": payload.get("exception_cause"),
            "pc": payload.get("pc"),
            "uptime_ms": payload.get("uptime_ms"),
            "crash_count": payload.get("crash_count"),
            "fw_version": payload.get("fw_version"),
            "build_id": payload.get("build_id"),
            "retained": False,
        },
        on_conflict="device_id,crash_count",
    ).execute()

    frames = payload.get("backtrace", [])
    if frames:
        # A frame is either a bare address string (a real ESP32 reports raw addresses,
        # symbolicated later via addr2line) or a {address, resolved} dict (the mock
        # reports pre-resolved frames so the dashboard backtrace is readable offline).
        rows = []
        for i, frame in enumerate(frames):
            if isinstance(frame, dict):
                addr = frame.get("address")
                resolved = frame.get("resolved")
            else:
                addr = frame
                resolved = None
            rows.append({"crash_id": crash_id, "position": i, "address": addr, "resolved": resolved})

        # Auto-symbolicate raw-address frames (real ESP32 / QEMU) when the build's ELF is staged,
        # so the dashboard/agent see resolved backtraces without a manual fleet_resolve_backtrace.
        # Best-effort: any failure (no ELF, no addr2line binary) leaves resolved=None — never drops
        # the crash. The mock's pre-resolved dict frames already have `resolved` and are left alone.
        build_id = payload.get("build_id")
        if build_id and any(r["resolved"] is None and r["address"] for r in rows):
            try:
                from nff_fleet import crash as _crash
                addrs = [r["address"] for r in rows]
                resolved_list = await _crash.resolve_frames(build_id, addrs)
                if resolved_list:
                    for r, res in zip(rows, resolved_list):
                        if r["resolved"] is None and res:
                            r["resolved"] = res
            except Exception as exc:  # noqa: BLE001 — resolution must never block ingest
                logger.warning("backtrace auto-resolve failed for build_id=%s: %s", build_id, exc)

        await _client.table("backtrace_frames").insert(rows).execute()

    rtc = payload.get("rtc_log", [])
    if rtc:
        await _client.table("rtc_log_lines").insert(
            [
                {
                    "crash_id": crash_id,
                    "position": i,
                    "level": entry.get("level"),
                    "tag": entry.get("tag"),
                    "message": entry.get("message"),
                    "ms_before_crash": entry.get("ms_before_crash"),
                }
                for i, entry in enumerate(rtc)
            ]
        ).execute()


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def append_audit(
    device_id: str,
    action: str,
    nonce: str,
    session_id: str,
    project_id: str,
    payload: dict[str, Any] | None = None,
) -> None:
    if not _enabled:
        return
    _fire(_append_audit(device_id, action, nonce, session_id, project_id, payload))


async def _append_audit(
    device_id: str,
    action: str,
    nonce: str,
    session_id: str,
    project_id: str,
    payload: dict[str, Any] | None,
) -> None:
    await _client.table("audit_log").insert(
        {
            "ts": _now(),
            "device_id": device_id,
            "project_id": project_id,   # NOT NULL in audit_log — every command is project-scoped
            "action": action,
            "nonce": nonce,
            "payload": {"session_id": session_id, **(payload or {})},
        }
    ).execute()


# ---------------------------------------------------------------------------
# Agent connected flag
# ---------------------------------------------------------------------------

async def set_agent_connected(connected: bool) -> None:
    if not _enabled:
        return
    try:
        await _client.table("devices").update(
            {"agent_connected": connected}
        ).neq("id", "").execute()
    except Exception as exc:
        logger.warning("db: set_agent_connected(%s) failed: %s", connected, exc)


# ---------------------------------------------------------------------------
# Service liveness
# ---------------------------------------------------------------------------

async def service_heartbeat(service: str, version: str | None = None) -> None:
    """Announce that this service is alive. The dashboard reads service_status
    and treats a service as offline once its heartbeat goes stale. Raises on
    failure so the caller's retry loop can log it."""
    if not _enabled:
        return
    await _client.rpc(
        "service_heartbeat",
        {"p_service": service, "p_version": version},
    ).execute()
