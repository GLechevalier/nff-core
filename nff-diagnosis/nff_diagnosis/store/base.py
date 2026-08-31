from abc import ABC, abstractmethod
from pathlib import Path

from nff_diagnosis.schemas import ELFRecord


class ELFStoreBase(ABC):
    @abstractmethod
    def put(
        self,
        elf_bytes: bytes,
        customer: str,
        project: str,
        firmware_version: str,
        target: str = "esp32",
        toolchain: str = "",
    ) -> ELFRecord: ...

    @abstractmethod
    def get_record(self, build_id: str) -> ELFRecord | None: ...

    @abstractmethod
    def get_elf_path(self, build_id: str) -> Path | None:
        """Returns local path to ELF file. Cloud impls download to temp first."""
        ...

    @abstractmethod
    def exists(self, build_id: str) -> bool: ...
