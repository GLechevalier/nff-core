"""Tests for the device-presence upsert, focused on device-attested identity.

nff-fleet has no pytest-asyncio config, so async code is driven via asyncio.run().
"""
import asyncio

import nff_fleet.db as db


class _FakeQuery:
    def __init__(self, captured: dict):
        self._captured = captured

    def execute(self):
        async def _exec():
            return None
        return _exec()


class _FakeInsert:
    def __init__(self, captured: dict):
        self._captured = captured

    def insert(self, row):
        self._captured["row"] = row
        return _FakeQuery(self._captured)


class _FakeClient:
    """Records the single rpc(name, params) call _upsert_device makes, and the
    table(...).insert(...) call _insert_telemetry makes."""

    def __init__(self, captured: dict):
        self._captured = captured

    def rpc(self, name, params):
        self._captured["name"] = name
        self._captured["params"] = params
        return _FakeQuery(self._captured)

    def table(self, name):
        self._captured["table"] = name
        return _FakeInsert(self._captured)


def test_upsert_device_forwards_identity(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(db, "_client", _FakeClient(captured))
    monkeypatch.setattr(db, "_enabled", True)

    payload = {
        "status": "online",
        "firmware_version": "2.0.0",
        "ip": "10.0.0.5",
        "rssi": -55,
        "uptime_seconds": 1234,
        "device_type": "esp32s3",
        "fqbn": "esp32:esp32:esp32s3",
        "chip": "ESP32-S3",
        "rev": 1,
        "flash": 8388608,
    }
    asyncio.run(db._upsert_device("dev-1", payload))

    assert captured["name"] == "upsert_device_heartbeat"
    p = captured["params"]
    assert p["p_id"] == "dev-1"
    assert p["p_device_type"] == "esp32s3"
    assert p["p_hardware"] == {
        "fqbn": "esp32:esp32:esp32s3",
        "chip": "ESP32-S3",
        "rev": 1,
        "flash": 8388608,
    }


def test_upsert_device_maps_real_heartbeat_keys(monkeypatch):
    """The firmware heartbeat sends "fw" and "uptime" (ms), not the DB-style names.
    _upsert_device must map them so Firmware/Uptime aren't stuck at "unknown"/0."""
    captured: dict = {}
    monkeypatch.setattr(db, "_client", _FakeClient(captured))
    monkeypatch.setattr(db, "_enabled", True)

    payload = {
        "status": "online",
        "id": "dev-1",
        "fw": "1.0.1",
        "uptime": 123456,          # milliseconds
        "device_type": "esp32s3",
        "fqbn": "esp32:esp32:esp32s3",
        "chip": "ESP32-S3",
        "rev": 1,
        "flash": 8388608,
    }
    asyncio.run(db._upsert_device("dev-1", payload))

    p = captured["params"]
    assert p["p_firmware_version"] == "1.0.1"
    assert p["p_uptime_seconds"] == 123  # 123456 ms // 1000
    assert p["p_device_type"] == "esp32s3"
    assert p["p_hardware"] == {
        "fqbn": "esp32:esp32:esp32s3",
        "chip": "ESP32-S3",
        "rev": 1,
        "flash": 8388608,
    }


def test_insert_telemetry_reads_heap_from_heartbeat(monkeypatch):
    """The heartbeat sends free heap as "heap"; telemetry stores it as heap_free."""
    captured: dict = {}
    monkeypatch.setattr(db, "_client", _FakeClient(captured))
    monkeypatch.setattr(db, "_enabled", True)

    asyncio.run(db._insert_telemetry("dev-1", {"status": "online", "heap": 131072, "rssi": -55}))

    assert captured["table"] == "device_telemetry"
    row = captured["row"]
    assert row["heap_free"] == 131072
    assert row["rssi"] == -55


def test_upsert_device_omits_absent_identity(monkeypatch):
    """A legacy heartbeat (no identity fields) sends null device_type and empty hardware,
    so the RPC's COALESCE preserves any previously-stored value instead of nulling it."""
    captured: dict = {}
    monkeypatch.setattr(db, "_client", _FakeClient(captured))
    monkeypatch.setattr(db, "_enabled", True)

    asyncio.run(db._upsert_device("dev-2", {"status": "online", "firmware_version": "1.0.0"}))

    p = captured["params"]
    assert p["p_device_type"] is None
    assert p["p_hardware"] == {}


def test_upsert_device_drops_blank_hardware_fields(monkeypatch):
    """Empty-string hardware fields are dropped, not forwarded as ''."""
    captured: dict = {}
    monkeypatch.setattr(db, "_client", _FakeClient(captured))
    monkeypatch.setattr(db, "_enabled", True)

    payload = {
        "status": "online",
        "firmware_version": "2.0.0",
        "device_type": "esp32",
        "fqbn": "",          # not built with a baked fqbn
        "chip": "ESP32-D0WD",
        "rev": 3,
        "flash": 4194304,
    }
    asyncio.run(db._upsert_device("dev-3", payload))

    p = captured["params"]
    assert p["p_device_type"] == "esp32"
    assert "fqbn" not in p["p_hardware"]
    assert p["p_hardware"] == {"chip": "ESP32-D0WD", "rev": 3, "flash": 4194304}
