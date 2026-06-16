from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nff_mock.device import VirtualDevice


def build_online(device: "VirtualDevice") -> dict[str, Any]:
    """The retained online heartbeat — field-for-field the SDK's publish_heartbeat()
    (nff_heartbeat.c), plus a few mock-friendly extras the backend already reads:
    `rssi`/`ip` (consumed by nff-fleet db.py telemetry) and `project_id` (consumed by
    the migration-0029 upsert), and any heartbeat fields the active firmware declares.
    """
    cfg = device.cfg
    t = device.telemetry
    payload: dict[str, Any] = {
        "status": "online",
        "id": cfg.device_id,
        "fw": device.fw_version,
        "build": device.build_id,
        "device_type": cfg.device_type,
        "fqbn": cfg.fqbn,
        "chip": cfg.chip,
        "rev": cfg.rev,
        "flash": cfg.flash,
        "heap": t.free_heap,
        "uptime": t.uptime_ms,
        "rssi": t.rssi,
        # Declares this device's heartbeat frequency so the server marks it offline
        # after two missed beats (2 × interval); field-for-field the SDK's interval_s.
        "interval_s": cfg.heartbeat_interval_s,
    }
    if cfg.ip:
        payload["ip"] = cfg.ip
    if cfg.project_id:
        payload["project_id"] = cfg.project_id
    payload.update(device.firmware.extra_heartbeat_fields)
    return payload


def build_offline(device: "VirtualDevice") -> dict[str, Any]:
    """The LWT / explicit-down payload (nff_mqtt.c registers this as the Last Will)."""
    payload = {"status": "offline", "id": device.cfg.device_id}
    if device.cfg.project_id:
        payload["project_id"] = device.cfg.project_id
    return payload
