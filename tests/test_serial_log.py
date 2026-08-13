"""Tests for the wired-link serial console read path (device_serial_logs).

nff-fleet has no pytest-asyncio config, so async code is driven via asyncio.run().
"""
import asyncio

import nff_fleet.db as db
from nff_fleet.tools.serial import fleet_get_serial_log


class _FakeSelectQuery:
    """Chainable stand-in for the supabase select builder; records every step."""

    def __init__(self, captured: dict, rows: list[dict]):
        self._captured = captured
        self._rows = rows

    def eq(self, col, val):
        self._captured.setdefault("eq", []).append((col, val))
        return self

    def gte(self, col, val):
        self._captured.setdefault("gte", []).append((col, val))
        return self

    def lt(self, col, val):
        self._captured.setdefault("lt", []).append((col, val))
        return self

    def ilike(self, col, pattern):
        self._captured.setdefault("ilike", []).append((col, pattern))
        return self

    def order(self, col, desc=False):
        self._captured.setdefault("order", []).append((col, desc))
        return self

    def limit(self, n):
        self._captured["limit"] = n
        return self

    def execute(self):
        rows = self._rows

        class _Resp:
            data = rows

        async def _exec():
            return _Resp()

        return _exec()


class _FakeTable:
    def __init__(self, captured: dict, rows: list[dict]):
        self._captured = captured
        self._rows = rows

    def select(self, cols):
        self._captured["select"] = cols
        return _FakeSelectQuery(self._captured, self._rows)

    def delete(self):
        self._captured["delete"] = True
        return _FakeSelectQuery(self._captured, [])


class _FakeClient:
    def __init__(self, captured: dict, rows: list[dict] | None = None):
        self._captured = captured
        self._rows = rows or []

    def table(self, name):
        self._captured["table"] = name
        return _FakeTable(self._captured, self._rows)


def test_get_serial_lines_query_shape_and_reversal(monkeypatch):
    """Query filters by device, optional since/grep, orders newest-first with a
    (ts, seq) tiebreak — and the result is reversed to oldest-first for reading."""
    captured: dict = {}
    rows = [  # as returned by the DB: newest first
        {"ts": "2026-08-13T10:00:02Z", "seq": 0, "line": "third"},
        {"ts": "2026-08-13T10:00:01Z", "seq": 1, "line": "second"},
        {"ts": "2026-08-13T10:00:01Z", "seq": 0, "line": "first"},
    ]
    monkeypatch.setattr(db, "_client", _FakeClient(captured, rows))
    monkeypatch.setattr(db, "_enabled", True)

    out = asyncio.run(
        db.get_serial_lines("dev-1", limit=50, since="2026-08-13T09:00:00Z", grep="mbedtls")
    )

    assert captured["table"] == "device_serial_logs"
    assert captured["eq"] == [("device_id", "dev-1")]
    assert captured["gte"] == [("ts", "2026-08-13T09:00:00Z")]
    assert captured["ilike"] == [("line", "%mbedtls%")]
    assert captured["order"] == [("ts", True), ("seq", True)]
    assert captured["limit"] == 50
    assert [r["line"] for r in out] == ["first", "second", "third"]


def test_get_serial_lines_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(db, "_enabled", False)
    assert asyncio.run(db.get_serial_lines("dev-1")) == []


def test_tool_clamps_limit(monkeypatch):
    seen: dict = {}

    async def fake_get(device_id, limit, since, grep):
        seen["limit"] = limit
        return [{"ts": "t", "seq": 0, "line": "x"}]

    monkeypatch.setattr(db, "get_serial_lines", fake_get)
    asyncio.run(fleet_get_serial_log("dev-1", limit=99999))
    assert seen["limit"] == 500
    asyncio.run(fleet_get_serial_log("dev-1", limit=-3))
    assert seen["limit"] == 1


def test_tool_empty_result_explains_wired_link(monkeypatch):
    async def fake_get(device_id, limit, since, grep):
        return []

    monkeypatch.setattr(db, "get_serial_lines", fake_get)
    out = asyncio.run(fleet_get_serial_log("dev-1"))
    assert out["count"] == 0
    assert "wired USB link" in out["message"]


def test_tool_truncated_flag(monkeypatch):
    async def fake_get(device_id, limit, since, grep):
        return [{"ts": "t", "seq": i, "line": str(i)} for i in range(limit)]

    monkeypatch.setattr(db, "get_serial_lines", fake_get)
    out = asyncio.run(fleet_get_serial_log("dev-1", limit=5))
    assert out["count"] == 5
    assert out["truncated"] is True


def test_prune_serial_logs_cutoff(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(db, "_client", _FakeClient(captured))
    monkeypatch.setattr(db, "_enabled", True)

    asyncio.run(db.prune_serial_logs(days=7))

    assert captured["table"] == "device_serial_logs"
    assert captured["delete"] is True
    (col, cutoff) = captured["lt"][0]
    assert col == "ts"
    from datetime import datetime, timedelta, timezone

    parsed = datetime.fromisoformat(cutoff)
    expect = datetime.now(timezone.utc) - timedelta(days=7)
    assert abs((parsed - expect).total_seconds()) < 60
