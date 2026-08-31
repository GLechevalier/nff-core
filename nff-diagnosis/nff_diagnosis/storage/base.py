from abc import ABC, abstractmethod

from nff_diagnosis.schemas import CrashRecord
from nff_diagnosis.schemas.diagnosis_output import DiagnosisOutput


class StorageBase(ABC):
    @abstractmethod
    def save_crash(self, record: CrashRecord) -> None: ...

    @abstractmethod
    def save_diagnosis(self, diag: DiagnosisOutput) -> None: ...

    @abstractmethod
    def get_crash(self, crash_id: str) -> CrashRecord | None: ...

    @abstractmethod
    def get_diagnosis(self, crash_id: str) -> DiagnosisOutput | None: ...

    @abstractmethod
    def list_crashes(self, limit: int = 50, offset: int = 0) -> list[CrashRecord]: ...

    def iter_all_crashes(self, batch: int = 500) -> list[CrashRecord]:
        """Return every stored crash (paged under the hood). Default impl pages
        list_crashes so existing backends get it for free."""
        out: list[CrashRecord] = []
        offset = 0
        while True:
            page = self.list_crashes(limit=batch, offset=offset)
            if not page:
                break
            out.extend(page)
            if len(page) < batch:
                break
            offset += batch
        return out
