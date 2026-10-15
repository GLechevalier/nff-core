"""build_crash_report parity/field tests.

The controller injects a prerecorded crash for a real L2 device by building the SAME wire
payload the L1 device self-reports with (nff_mock_core.scenarios.build_crash_report). These
tests pin the payload's field names to what nff-fleet's db.ingest_crash consumes, so an
injected crash and a self-reported one can never drift.
"""

from __future__ import annotations

import pytest

from nff_mock_core import scenarios

# The columns db._ingest_crash reads off the payload (nff-fleet/nff_fleet/db.py::_ingest_crash).
_CRASH_REPORT_FIELDS = {
    "reason", "exception_cause", "pc", "uptime_ms", "crash_count",
    "fw_version", "build_id", "backtrace", "rtc_log",
}


@pytest.mark.parametrize("sid", list(scenarios.SCENARIOS))
def test_build_crash_report_has_ingest_fields(sid: str) -> None:
    _, sc = scenarios.resolve(sid)
    report = scenarios.build_crash_report(
        "mock-abc-123", sid, sc,
        fw="5.1.0", build_id="deadbeefcafe0001", uptime_ms=4200, crash_count=2,
    )
    # Every field db.ingest_crash reads must be present.
    assert _CRASH_REPORT_FIELDS <= set(report), (
        f"{sid}: missing {_CRASH_REPORT_FIELDS - set(report)}"
    )
    assert report["crash_count"] == 2
    assert report["fw_version"] == "5.1.0"
    assert report["build_id"] == "deadbeefcafe0001"
    assert report["type"] == "crash"
    assert report["id"] == "mock-abc-123"


@pytest.mark.parametrize("sid", list(scenarios.SCENARIOS))
def test_backtrace_frames_are_ingestible(sid: str) -> None:
    _, sc = scenarios.resolve(sid)
    report = scenarios.build_crash_report(
        "mock-x", sid, sc, fw="1.2.3", build_id="b" * 16, uptime_ms=1, crash_count=1,
    )
    frames = report["backtrace"]
    assert isinstance(frames, list) and frames, f"{sid}: empty backtrace"
    # db.ingest_crash accepts {address, resolved} dicts (mock) or bare address strings.
    for f in frames:
        assert isinstance(f, dict) and "address" in f, f"{sid}: bad frame {f!r}"
    # rtc_log rows carry the level/tag/message/ms_before_crash db.ingest_crash expects.
    for line in report["rtc_log"]:
        assert {"level", "tag", "message", "ms_before_crash"} <= set(line)


@pytest.mark.parametrize("sid", list(scenarios.SCENARIOS))
def test_raw_is_present_and_stamped(sid: str) -> None:
    _, sc = scenarios.resolve(sid)
    report = scenarios.build_crash_report(
        "mock-x", sid, sc, fw="9.9.9", build_id="feedface00001234", uptime_ms=777, crash_count=1,
    )
    raw = report["raw"]
    # The raw dump is what nff-diagnosis diagnose_serial parses; it must carry the build id so it
    # matches get_firmware_source, and be a non-trivial panic dump.
    assert isinstance(raw, str) and "feedface00001234" in raw
    assert len(raw) > 40


def test_build_crash_report_matches_device_self_report_shape() -> None:
    """The controller-injected payload has the exact keys the device self-report builds — they are
    the same function now, so this pins that contract against accidental divergence."""
    sid, sc = scenarios.resolve("null_deref")
    injected = scenarios.build_crash_report(
        "mock-x", sid, sc, fw="5.1.0", build_id="a" * 16, uptime_ms=100, crash_count=3,
    )
    # The device's check_and_report builds the identical dict via this same helper; assert the
    # full key set (guards against a field being added to one path but not the other).
    expected_keys = {
        "type", "id", "fw", "fw_version", "build", "build_id", "reason", "exception_cause",
        "pc", "fault_addr", "task_name", "panic_type", "crash_count", "uptime_ms",
        "backtrace", "rtc_log", "raw",
    }
    assert set(injected) == expected_keys
