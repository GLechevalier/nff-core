from __future__ import annotations

from pathlib import Path

from nff_diagnosis.schemas import TimelineEntry
from nff_diagnosis.storage.base import StorageBase
from nff_diagnosis.store.base import ELFStoreBase


class TimelineBuilder:
    """Materializes the crash-timeline — the single source of truth for who crashed
    and when — by joining each crash with its diagnosis and the ELF behind its
    build_id (for firmware_version / project / customer).
    """

    def __init__(self, storage: StorageBase, store: ELFStoreBase) -> None:
        self._storage = storage
        self._store = store

    def build(self) -> list[TimelineEntry]:
        entries: list[TimelineEntry] = []
        for crash in self._storage.iter_all_crashes():
            diag = self._storage.get_diagnosis(crash.crash_id)
            elf = self._store.get_record(crash.build_id)
            entries.append(
                TimelineEntry(
                    crash_id=crash.crash_id,
                    device_id=crash.device_id,
                    build_id=crash.build_id,
                    firmware_version=elf.firmware_version if elf else None,
                    project=elf.project if elf else None,
                    customer=elf.customer if elf else None,
                    received_at=crash.received_at,
                    panic_type=crash.panic_type,
                    crash_class=diag.crash_class if diag else None,
                    location=diag.location if diag else {},
                    confidence=diag.confidence if diag else None,
                )
            )
        entries.sort(key=lambda e: e.received_at)
        return entries

    @staticmethod
    def write_jsonl(entries: list[TimelineEntry], path: Path) -> Path:
        """Append-only write: merge new entries into the file, deduped by crash_id,
        keeping the file the durable source of truth across runs."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        merged: dict[str, TimelineEntry] = {
            e.crash_id: e for e in TimelineBuilder.read_jsonl(path)
        }
        for e in entries:
            merged[e.crash_id] = e

        ordered = sorted(merged.values(), key=lambda e: e.received_at)
        with path.open("w", encoding="utf-8") as fh:
            for e in ordered:
                fh.write(e.model_dump_json() + "\n")
        return path

    @staticmethod
    def read_jsonl(path: Path) -> list[TimelineEntry]:
        path = Path(path)
        if not path.exists():
            return []
        out: list[TimelineEntry] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(TimelineEntry.model_validate_json(line))
        return out
