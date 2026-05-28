from nff_diagnosis.store.local import LocalELFStore


def test_put_and_get(tmp_store: LocalELFStore):
    elf_bytes = b"\x7fELF" + b"\x00" * 100
    record = tmp_store.put(
        elf_bytes=elf_bytes,
        customer="acme",
        project="sensor",
        firmware_version="1.0.0",
    )
    assert record.build_id
    assert tmp_store.exists(record.build_id)
    fetched = tmp_store.get_record(record.build_id)
    assert fetched is not None
    assert fetched.build_id == record.build_id
    assert fetched.customer == "acme"


def test_put_idempotent(tmp_store: LocalELFStore):
    elf_bytes = b"\x7fELF" + b"\x00" * 50
    r1 = tmp_store.put(elf_bytes, "c", "p", "1.0")
    r2 = tmp_store.put(elf_bytes, "c", "p", "1.0")
    assert r1.build_id == r2.build_id


def test_get_elf_path(tmp_store: LocalELFStore):
    elf_bytes = b"\x7fELF" + b"\x01" * 50
    record = tmp_store.put(elf_bytes, "c", "p", "1.0")
    path = tmp_store.get_elf_path(record.build_id)
    assert path is not None
    assert path.exists()
    assert path.read_bytes() == elf_bytes


def test_missing_build_id(tmp_store: LocalELFStore):
    assert tmp_store.get_record("nonexistent") is None
    assert tmp_store.get_elf_path("nonexistent") is None
    assert not tmp_store.exists("nonexistent")
