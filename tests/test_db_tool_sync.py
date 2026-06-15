"""The tool-sync payload builder + the offline posture of the DB layer."""

from __future__ import annotations

from nff_pentester import db


def test_tool_rows_one_present_row_per_tool():
    tools = [("port_scan", "Scan ports"), ("walk_vuln_tree", None)]
    rows = db.tool_rows(tools)
    assert len(rows) == 2
    assert {r["name"] for r in rows} == {"port_scan", "walk_vuln_tree"}
    assert all(r["present"] is True and r["origin"] == "registry" for r in rows)
    # description is carried through, including None.
    assert {r["name"]: r["description"] for r in rows} == {
        "port_scan": "Scan ports",
        "walk_vuln_tree": None,
    }


def test_not_configured_without_supabase_env(monkeypatch):
    # With no Supabase URL/key, the DB layer reports unconfigured so the walker
    # falls back to the bundled JSON (offline / unit-test posture).
    monkeypatch.setattr(db.settings, "supabase_url", "")
    monkeypatch.setattr(db.settings, "supabase_service_role_key", "")
    assert db.configured() is False
