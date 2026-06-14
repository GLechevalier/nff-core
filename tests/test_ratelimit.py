"""Unit tests for the shared RateLimitDetector."""

from nff_pentester.utils.ratelimit import RateLimitDetector


def test_trips_on_429():
    d = RateLimitDetector()
    d.observe(200, {})
    assert d.tripped is False
    d.observe(429, {})
    assert d.tripped is True
    assert "429" in (d.reason or "")


def test_trips_on_retry_after_header():
    d = RateLimitDetector()
    d.observe(200, {"Retry-After": "30"})
    assert d.tripped is True
    assert "Retry-After" in (d.reason or "")


def test_trips_after_consecutive_errors():
    d = RateLimitDetector(max_consecutive_errors=3)
    d.note_error()
    d.note_error()
    assert d.tripped is False
    d.note_error()
    assert d.tripped is True


def test_successful_response_resets_error_run():
    d = RateLimitDetector(max_consecutive_errors=3)
    d.note_error()
    d.note_error()
    d.observe(200, {})  # resets the run
    d.note_error()
    d.note_error()
    assert d.tripped is False


async def test_backoff_only_fires_once():
    d = RateLimitDetector(backoff_seconds=0)  # no real sleep in tests
    assert await d.backoff() is True
    assert await d.backoff() is False
