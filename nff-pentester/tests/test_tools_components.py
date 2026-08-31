"""Unit tests for the components domain: cve_lookup and cve_db_sync."""

from nff_pentester.config import settings
from nff_pentester.tools.components.cve import CveLookupInput, cve_lookup
from nff_pentester.tools.components.cve_sync import CveDbSyncInput, cve_db_sync


# ── cve_lookup ──────────────────────────────────────────────────────────────────

async def test_cve_lookup_matches_dropbear(mock_ctx):
    res = await cve_lookup(CveLookupInput(service="dropbear", version="2016.74"), mock_ctx)
    assert res.match_count >= 1
    assert any(c.id == "CVE-2016-7406" for c in res.matched_cves)


# ── cve_db_sync (offline by default) ────────────────────────────────────────────

async def test_cve_db_sync_refuses_in_demo_mode(mock_ctx, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "cve_sync_enabled", True)
    monkeypatch.setattr(settings, "cve_feed_url", "http://feed.example/cves.json")
    res = await cve_db_sync(CveDbSyncInput(), mock_ctx)
    assert res.error is not None
    assert "demo mode" in res.error
    assert res.added == 0


async def test_cve_db_sync_refuses_when_disabled(mock_ctx, monkeypatch):
    # Sandbox/demo off (autouse), but sync disabled by default.
    monkeypatch.setattr(settings, "cve_sync_enabled", False)
    res = await cve_db_sync(CveDbSyncInput(feed_url="http://feed.example/cves.json"), mock_ctx)
    assert res.error is not None
    assert "disabled" in res.error


async def test_cve_db_sync_needs_feed_url(mock_ctx, monkeypatch):
    monkeypatch.setattr(settings, "cve_sync_enabled", True)
    monkeypatch.setattr(settings, "cve_feed_url", "")
    res = await cve_db_sync(CveDbSyncInput(), mock_ctx)
    assert res.error is not None
    assert "feed URL" in res.error
