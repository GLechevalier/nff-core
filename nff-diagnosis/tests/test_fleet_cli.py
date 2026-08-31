import json
from pathlib import Path

from click.testing import CliRunner

from nff_diagnosis.cli.main import cli
from nff_diagnosis.schemas import CrashRecord, DiagnosisOutput
from nff_diagnosis.storage.sqlite import SQLiteStorage
from nff_diagnosis.store.local import LocalELFStore

FIXTURES = Path(__file__).parent / "fixtures"


def _seed(store_root: Path, db_path: Path) -> None:
    store = LocalELFStore(store_root)
    storage = SQLiteStorage(db_path)
    for fw, device, cc in [("3.4", "dev-34a", "null_deref"), ("3.5", "dev-35a", "null_deref")]:
        rec = store.put(elf_bytes=f"elf-{fw}".encode(), customer="acme", project="thermo", firmware_version=fw)
        crash = CrashRecord(build_id=rec.build_id, device_id=device, panic_type="guru_meditation", raw="x")
        storage.save_crash(crash)
        storage.save_diagnosis(
            DiagnosisOutput(
                crash_id=crash.crash_id,
                crash_class=cc,
                root_cause="r",
                location={"function": "poll_sensors", "file": "s.c", "line": 88},
                resolved_backtrace=[],
                confidence=0.9,
                engine_version="t",
            )
        )


def _global_args(tmp_path: Path) -> list[str]:
    return ["--store-root", str(tmp_path / "store"), "--db", str(tmp_path / "db.sqlite")]


def test_fleet_timeline_cli(tmp_path: Path):
    _seed(tmp_path / "store", tmp_path / "db.sqlite")
    out = tmp_path / "timeline.jsonl"
    runner = CliRunner()
    res = runner.invoke(cli, [*_global_args(tmp_path), "fleet", "timeline", "--out", str(out)])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["total_crashes"] == 2
    assert data["by_crash_class"]["null_deref"] == 2
    assert out.exists()


def test_fleet_correlate_cli_no_llm(tmp_path: Path):
    _seed(tmp_path / "store", tmp_path / "db.sqlite")
    report_out = tmp_path / "report.json"
    runner = CliRunner()
    res = runner.invoke(
        cli,
        [
            *_global_args(tmp_path),
            "fleet",
            "correlate",
            "--roster",
            str(FIXTURES / "fleet_roster.json"),
            "--no-llm",
            "--out",
            str(report_out),
        ],
    )
    assert res.exit_code == 0, res.output
    report = json.loads(res.output)
    assert report["total_crashes"] == 2
    assert report["engine_version"].startswith("nff-diagnosis-fleet/")
    assert report_out.exists()
