from pathlib import Path

from nff_diagnosis.correlation.timeline import TimelineBuilder
from nff_diagnosis.schemas import CrashRecord, DiagnosisOutput
from nff_diagnosis.storage.sqlite import SQLiteStorage
from nff_diagnosis.store.local import LocalELFStore


def _seed(store: LocalELFStore, storage: SQLiteStorage, fw: str, crash_class: str) -> str:
    rec = store.put(
        elf_bytes=f"elf-{fw}".encode(),
        customer="acme",
        project="thermostat",
        firmware_version=fw,
    )
    crash = CrashRecord(build_id=rec.build_id, device_id=f"dev-{fw}", panic_type="guru_meditation", raw="...")
    storage.save_crash(crash)
    storage.save_diagnosis(
        DiagnosisOutput(
            crash_id=crash.crash_id,
            crash_class=crash_class,
            root_cause="because",
            location={"function": "poll_sensors", "file": "sensors.c", "line": 88},
            resolved_backtrace=[],
            confidence=0.9,
            engine_version="test",
        )
    )
    return crash.crash_id


def test_build_joins_firmware_and_class(tmp_store: LocalELFStore, tmp_storage: SQLiteStorage):
    _seed(tmp_store, tmp_storage, "3.4", "null_deref")
    _seed(tmp_store, tmp_storage, "3.6", "watchdog")

    entries = TimelineBuilder(tmp_storage, tmp_store).build()
    assert len(entries) == 2
    by_fw = {e.firmware_version: e for e in entries}
    assert by_fw["3.4"].crash_class == "null_deref"
    assert by_fw["3.4"].location["function"] == "poll_sensors"
    assert by_fw["3.4"].project == "thermostat"
    assert by_fw["3.4"].customer == "acme"
    assert by_fw["3.6"].crash_class == "watchdog"
    assert by_fw["3.4"].confidence == 0.9


def test_undiagnosed_crash_has_null_class(tmp_store: LocalELFStore, tmp_storage: SQLiteStorage):
    rec = tmp_store.put(elf_bytes=b"elf", customer="c", project="p", firmware_version="1.0")
    crash = CrashRecord(build_id=rec.build_id, panic_type="unknown", raw="x")
    tmp_storage.save_crash(crash)  # no diagnosis saved
    entries = TimelineBuilder(tmp_storage, tmp_store).build()
    assert len(entries) == 1
    assert entries[0].crash_class is None
    assert entries[0].firmware_version == "1.0"


def test_jsonl_roundtrip_and_dedup(tmp_store: LocalELFStore, tmp_storage: SQLiteStorage, tmp_path: Path):
    _seed(tmp_store, tmp_storage, "3.4", "null_deref")
    builder = TimelineBuilder(tmp_storage, tmp_store)
    out = tmp_path / "fleet" / "timeline.jsonl"

    entries = builder.build()
    builder.write_jsonl(entries, out)
    # Re-write the same entries — append-only merge must dedup by crash_id.
    builder.write_jsonl(entries, out)

    loaded = TimelineBuilder.read_jsonl(out)
    assert len(loaded) == 1
    assert loaded[0].crash_id == entries[0].crash_id
