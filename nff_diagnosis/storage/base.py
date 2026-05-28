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
