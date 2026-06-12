from __future__ import annotations

from nff_diagnosis.schemas import TemporalCluster, TimelineEntry


def cluster_by_time(
    entries: list[TimelineEntry],
    window_seconds: float,
) -> list[TemporalCluster]:
    """Group crashes that happened 'at the same time'. Entries are walked in
    chronological order; a new cluster starts whenever the gap to the previous
    crash exceeds `window_seconds`. Answers 'how many crashed together'."""
    ordered = sorted(entries, key=lambda e: e.received_at)
    clusters: list[TemporalCluster] = []
    current: list[TimelineEntry] = []

    def flush(group: list[TimelineEntry]) -> None:
        if not group:
            return
        clusters.append(
            TemporalCluster(
                cluster_id=f"cluster-{len(clusters)}",
                start=group[0].received_at,
                end=group[-1].received_at,
                size=len(group),
                crash_ids=[e.crash_id for e in group],
            )
        )

    for entry in ordered:
        if current:
            gap = (entry.received_at - current[-1].received_at).total_seconds()
            if gap > window_seconds:
                flush(current)
                current = []
        current.append(entry)
    flush(current)
    return clusters
