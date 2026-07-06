from pathlib import Path

import pytest

from nff_diagnosis.store.local import LocalELFStore
from nff_diagnosis.storage.sqlite import SQLiteStorage

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def tmp_store(tmp_path: Path) -> LocalELFStore:
    return LocalELFStore(tmp_path / "store")


@pytest.fixture()
def tmp_storage(tmp_path: Path) -> SQLiteStorage:
    return SQLiteStorage(tmp_path / "test.db")


@pytest.fixture()
def null_deref_raw() -> str:
    return (FIXTURES / "crash_null_deref.txt").read_text(encoding="utf-8")


@pytest.fixture()
def stack_overflow_raw() -> str:
    return (FIXTURES / "crash_stack_overflow.txt").read_text(encoding="utf-8")


@pytest.fixture()
def watchdog_raw() -> str:
    return (FIXTURES / "crash_watchdog.txt").read_text(encoding="utf-8")


@pytest.fixture()
def heap_corrupt_raw() -> str:
    return (FIXTURES / "crash_heap_corrupt.txt").read_text(encoding="utf-8")


@pytest.fixture()
def divide_by_zero_raw() -> str:
    return (FIXTURES / "crash_divide_by_zero.txt").read_text(encoding="utf-8")


@pytest.fixture()
def unaligned_access_raw() -> str:
    return (FIXTURES / "crash_unaligned_access.txt").read_text(encoding="utf-8")


@pytest.fixture()
def bad_instruction_fetch_raw() -> str:
    return (FIXTURES / "crash_bad_instruction_fetch.txt").read_text(encoding="utf-8")


@pytest.fixture()
def illegal_instruction_raw() -> str:
    return (FIXTURES / "crash_illegal_instruction.txt").read_text(encoding="utf-8")


@pytest.fixture()
def assert_failed_raw() -> str:
    return (FIXTURES / "crash_assert_failed.txt").read_text(encoding="utf-8")


@pytest.fixture()
def abort_bare_raw() -> str:
    return (FIXTURES / "crash_abort_bare.txt").read_text(encoding="utf-8")


@pytest.fixture()
def brownout_raw() -> str:
    return (FIXTURES / "crash_brownout.txt").read_text(encoding="utf-8")


@pytest.fixture()
def watchdog_blocking_raw() -> str:
    return (FIXTURES / "crash_watchdog_blocking.txt").read_text(encoding="utf-8")
