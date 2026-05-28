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
