from datetime import UTC, datetime, timedelta

from nff_diagnosis.correlation.cluster import cluster_by_time
from nff_diagnosis.schemas import TimelineEntry

_T0 = datetime(2026, 6, 12, 10, 0, 0, tzinfo=UTC)


def _entry(idx: int, offset_seconds: float) -> TimelineEntry:
    return TimelineEntry(
        crash_id=f"c{idx}",
        build_id="b",
        received_at=_T0 + timedelta(seconds=offset_seconds),
        panic_type="guru_meditation",
    )


def test_clusters_split_on_window_gap():
    # Three crashes within 60s, then a big gap, then two more close together.
    entries = [
        _entry(0, 0),
        _entry(1, 30),
        _entry(2, 60),
        _entry(3, 1000),
        _entry(4, 1010),
    ]
    clusters = cluster_by_time(entries, window_seconds=300)
    assert len(clusters) == 2
    assert clusters[0].size == 3
    assert clusters[0].crash_ids == ["c0", "c1", "c2"]
    assert clusters[1].size == 2


def test_single_cluster_when_within_window():
    entries = [_entry(i, i * 10) for i in range(5)]
    clusters = cluster_by_time(entries, window_seconds=300)
    assert len(clusters) == 1
    assert clusters[0].size == 5


def test_empty_timeline():
    assert cluster_by_time([], window_seconds=300) == []
