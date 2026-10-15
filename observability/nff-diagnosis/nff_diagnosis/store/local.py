import hashlib
import json
from pathlib import Path

from nff_diagnosis.schemas import ELFRecord
from nff_diagnosis.store.base import ELFStoreBase


class LocalELFStore(ELFStoreBase):
    """Filesystem ELF store. Layout: root/{customer}/{project}/{build_id}/"""

    def __init__(self, store_root: Path) -> None:
        self._root = Path(store_root)

    def _build_id(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _dir(self, record: ELFRecord) -> Path:
        return self._root / record.customer / record.project / record.build_id

    def _dir_by_id(self, build_id: str) -> Path | None:
        for customer_dir in self._root.iterdir() if self._root.exists() else []:
            for project_dir in customer_dir.iterdir():
                candidate = project_dir / build_id
                if candidate.is_dir():
                    return candidate
        return None

    def put(
        self,
        elf_bytes: bytes,
        customer: str,
        project: str,
        firmware_version: str,
        target: str = "esp32",
        toolchain: str = "",
    ) -> ELFRecord:
        build_id = self._build_id(elf_bytes)
        record = ELFRecord(
            build_id=build_id,
            elf_path="",  # filled in below
            target=target,
            firmware_version=firmware_version,
            project=project,
            customer=customer,
            toolchain=toolchain,
        )
        d = self._dir(record)
        if d.exists():
            return self.get_record(build_id)  # type: ignore[return-value]
        d.mkdir(parents=True, exist_ok=True)
        elf_file = d / "firmware.elf"
        elf_file.write_bytes(elf_bytes)
        record = record.model_copy(update={"elf_path": str(elf_file)})
        (d / "record.json").write_text(record.model_dump_json(), encoding="utf-8")
        return record

    def get_record(self, build_id: str) -> ELFRecord | None:
        d = self._dir_by_id(build_id)
        if d is None:
            return None
        record_file = d / "record.json"
        if not record_file.exists():
            return None
        return ELFRecord.model_validate_json(record_file.read_text(encoding="utf-8"))

    def get_elf_path(self, build_id: str) -> Path | None:
        d = self._dir_by_id(build_id)
        if d is None:
            return None
        elf_file = d / "firmware.elf"
        return elf_file if elf_file.exists() else None

    def exists(self, build_id: str) -> bool:
        return self._dir_by_id(build_id) is not None
