import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nff_diagnosis.cli.main import cli
from nff_diagnosis.schemas import CrashRecord

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def runner():
    return CliRunner()


def test_elf_upload_and_show(runner: CliRunner, tmp_path: Path):
    store_root = str(tmp_path / "store")
    elf_file = tmp_path / "fw.elf"
    elf_file.write_bytes(b"\x7fELF" + b"\x00" * 64)

    result = runner.invoke(cli, [
        "--store-root", store_root,
        "elf", "upload",
        "--elf", str(elf_file),
        "--customer", "acme",
        "--project", "sensor",
        "--firmware-version", "1.0.0",
    ])
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    build_id = record["build_id"]

    result2 = runner.invoke(cli, ["--store-root", store_root, "elf", "show", build_id])
    assert result2.exit_code == 0
    assert json.loads(result2.output)["build_id"] == build_id


def test_capture_from_file(runner: CliRunner, tmp_path: Path):
    result = runner.invoke(cli, [
        "capture", "from-file",
        "--raw", str(FIXTURES / "crash_null_deref.txt"),
        "--build-id", "abc123",
    ])
    assert result.exit_code == 0, result.output
    record = CrashRecord.model_validate_json(result.output)
    assert record.build_id == "abc123"
    assert record.raw != ""
    assert record.panic_type == "guru_meditation"


def test_capture_from_file_watchdog(runner: CliRunner):
    result = runner.invoke(cli, [
        "capture", "from-file",
        "--raw", str(FIXTURES / "crash_watchdog.txt"),
        "--build-id", "wdt001",
    ])
    assert result.exit_code == 0
    record = CrashRecord.model_validate_json(result.output)
    assert record.panic_type == "idf_watchdog"
    assert record.backtrace == []


def test_elf_show_missing(runner: CliRunner, tmp_path: Path):
    result = runner.invoke(cli, [
        "--store-root", str(tmp_path / "store"),
        "elf", "show", "nonexistent",
    ])
    assert result.exit_code != 0
    assert "ERROR" in result.output or "ERROR" in (result.output + str(result.exception or ""))
