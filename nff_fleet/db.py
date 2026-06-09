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
        }
    ).execute()


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
        await _client.table("backtrace_frames").insert(
            [
                {"crash_id": crash_id, "position": i, "address": addr, "resolved": None}
                for i, addr in enumerate(frames)
            ]
        ).execute()

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
    payload: dict[str, Any] | None = None,
) -> None:
    if not _enabled:
        return
    _fire(_append_audit(device_id, action, nonce, session_id, payload))


async def _append_audit(
    device_id: str,
    action: str,
    nonce: str,
    session_id: str,
    payload: dict[str, Any] | None,
) -> None:
    await _client.table("audit_log").insert(
        {
            "ts": _now(),
            "device_id": device_id,
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
